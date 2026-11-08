"""Issue #1205: the occupant's fan-off ALWAYS starts the fan-off grace (hold); the #829 marker is label-only.

Occupant framing: someone in the home switches off a fan (the whole-house fan in ``whole_house_fan`` mode, the
thermostat blower in ``hvac_fan`` mode) that Climate Advisor started, took over or restarted. Before #1205, if the
run traced back to an earlier manual fan-on (the #829 marker), CA read the off as "closing the earlier session",
started NO hold and, with a window open and cool air outside, switched the fan back on within ~6-13 minutes.

Owner decision (2026-11-01): CA always knows how a run should end. A selected RF timer is tracked by time (its off is
the #530 settle window); with no timer CA applies a default timer and at its expiry the reconcile either turns the
fan off itself (``turn-off``, CA's own command) or keeps it as CA's own run (``adopt-on``). So every fan-off CA did
not command, outside the settle window, is a PERSON and starts the normal fan-off grace. The marker
(``_fan_manual_origin_unclosed`` + run binding / pending fact) only selects the Activity Record wording
(``fan_cancel`` trigger ``manual_origin_carryforward`` vs ``fan_off``).

Every scenario asserts what the occupant notices: the fan STAYS OFF for the whole hold. The helper
``_assert_fan_held_off`` drives the real periodic checks (``check_natural_vent_conditions`` and
``nat_vent_temperature_check``) across the whole grace with the window open and nat-vent conditions favourable, and
fails with ``_OccupantOutcomeViolated`` if a fan-on is ever commanded before the grace ends. Route preconditions use
plain asserts so a broken precondition is not mistaken for the symptom.

Scenarios (each in both supported fan modes; ``FAN_MODE_BOTH`` is unsupported, #1175): 1 ordinary start then off;
2 R1 (abandoned deferral leaves a stale marker) then an unrelated start; 3 R2 (a marked run stopped by CA) then an
unrelated start; 4 manual no-timer fan-on, default grace expires, reconcile ``adopt-on``; 5 A4 grace-expiry
reactivation; 6 A3 deferred then applied (PAUSED and PAUSED_IDLE); 7 CA cycle-off/on inside a marked run;
8 the #530 settle window and the reconcile ``turn-off`` stay NO hold.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor.const import (
    CONF_FAN_MODE,
    CONF_MANUAL_GRACE_PERIOD,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    FAN_MIN_TOGGLE_INTERVAL_S,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
)
from tests.test_restore_resolved_band import _classification, _make_engine

_AUTOMATION = "custom_components.climate_advisor.automation"
_T0 = datetime(2026, 10, 28, 14, 0, 0, tzinfo=UTC)
_SENSOR = "binary_sensor.front_window"
_MODES = [FAN_MODE_WHOLE_HOUSE, FAN_MODE_HVAC]


class _OccupantOutcomeViolated(AssertionError):
    """The occupant's off of the unrelated session was ignored (the #1205 symptom)."""


# ---------------------------------------------------------------------------
# Harness: a real engine, a virtual clock, a window the test opens and closes
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = _T0

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _Home:
    """One real ``AutomationEngine`` plus the virtual clock and the window the scenario drives."""

    def __init__(self, fan_mode: str, clock: _Clock, **config) -> None:
        self.fan_mode = fan_mode
        self.clock = clock
        engine = _make_engine(indoor=72.0, mode="cool", **{CONF_FAN_MODE: fan_mode}, **config)
        engine._test_state.attributes["fan_modes"] = ["auto", "on"]
        engine._last_outdoor_temp = 65.0  # mild day: outdoor < indoor, plain nat-vent entry gate satisfied
        engine.inputs.hourly_forecast = []
        engine._current_classification = _classification("mild")
        self.window_open = True
        engine._sensor_check_callback = lambda: self.window_open
        engine._sensor_debounce_pending_callback = lambda: False
        self.events: list[tuple[str, dict]] = []
        self.timed_events: list[tuple[datetime, str, dict]] = []  # the real event stream with the virtual clock's time

        def _record(name: str, data: dict) -> None:
            self.events.append((name, data))
            self.timed_events.append((self.clock.now, name, data))

        engine._emit_event_callback = _record
        self._tasks: list = []
        engine.hass.async_create_task.side_effect = self._tasks.append
        self.engine = engine

    @property
    def hvac_fan(self) -> bool:
        return self.fan_mode == FAN_MODE_HVAC

    def drain(self) -> None:
        """Run every task the engine scheduled (the engine's own follow-ups), like the HA loop would."""
        while self._tasks:
            asyncio.run(self._tasks.pop(0))

    def open_window(self) -> None:
        """The window opens and stays open past the sensor debounce; then the post-debounce handler runs."""
        self.window_open = True
        self.clock.advance(DEFAULT_SENSOR_DEBOUNCE_SECONDS)
        asyncio.run(self.engine.handle_door_window_open(_SENSOR))
        self.drain()

    def close_window(self) -> None:
        self.window_open = False
        asyncio.run(self.engine.handle_all_doors_windows_closed())
        self.drain()

    def expire_grace(self) -> None:
        """Run the real grace-expiry callback, then the re-pause task it schedules."""
        engine = self.engine
        self.clock.advance(max(0.0, (datetime.fromisoformat(engine._grace_end_time) - self.clock.now).total_seconds()))
        engine._on_grace_expired(engine._last_resume_source, engine._grace_duration_seconds, False)
        self.drain()

    def occupant_turns_fan_off(self) -> None:
        self.engine.on_fan_turned_off(fan_before="on", fan_after="off", hvac_fan=self.hvac_fan)
        self.drain()

    def event_names(self) -> list[str]:
        return [n for n, _ in self.events]

    def cancel_triggers(self) -> list[str]:
        return [d.get("trigger") for n, d in self.events if n == "fan_cancel"]


@pytest.fixture
def clock():
    clk = _Clock()
    with (
        patch(f"{_AUTOMATION}.dt_util.now", side_effect=lambda: clk.now),
        patch(f"{_AUTOMATION}.dt_util.parse_datetime", side_effect=datetime.fromisoformat),
    ):
        yield clk


def _start_automation_grace_with_window_open(home: _Home) -> None:
    """A CA grace is running with the window open (e.g. after a nat-vent exit resume)."""
    home.engine._start_grace_period("automation", "nat_vent_exit_resume")
    assert home.engine._grace_active


def _grace_expires_inside_the_toggle_floor(home: _Home) -> None:
    """CA toggled the fan a few seconds before the grace ends, so the 300 s floor is still running at expiry."""
    engine = home.engine
    remaining = (datetime.fromisoformat(engine._grace_end_time) - home.clock.now).total_seconds()
    home.clock.advance(remaining - 10)
    engine._fan_toggle_command_time = home.clock.now  # stamped by CA's own last real fan command
    home.expire_grace()


def _unrelated_ordinary_ca_start(home: _Home) -> None:
    """The window opens again (past the debounce) with the toggle floor long gone and CA starts nat-vent."""
    engine = home.engine
    assert not home.window_open
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    home.open_window()
    assert engine._fan_active, "precondition: an ordinary CA nat-vent start must run the fan"
    assert engine._natural_vent_active
    assert "fan_activated" in home.event_names()


def _fan_on_commands(home: _Home) -> list:
    """Every fan-ON service call the engine has issued (whole-house fan turn_on, blower set_fan_mode on)."""
    calls = home.engine.hass.services.async_call.await_args_list
    out = []
    for c in calls:
        domain, service, data = c.args[0], c.args[1], c.args[2] if len(c.args) > 2 else {}
        if service == "turn_on" and domain != "climate" or service == "set_fan_mode" and data.get("fan_mode") == "on":
            out.append(c)
    return out


_HOLD_STEP_S = 240


def _assert_fan_held_off(
    home: _Home,
    *,
    matched_kind: str | None = None,
    indoor: float = 72.0,
    hold_end: datetime | None = None,
    tick: Any = None,
    economizer: bool = False,
) -> None:
    """The occupant's off started the fan-off grace (hold) and CA never commands the fan on until it ends.

    Drives the real periodic checks across the whole hold with the window open and nat-vent conditions favourable.
    ``matched_kind`` is the manual-origin label kind ("takeover"/"restart") the Activity Record must carry, or None
    for the plain ``fan_off`` wording. ``indoor`` is the house temperature the periodic checks see (Issue #134: a
    house above ``comfort_cool`` must not buy the fan back inside the occupant's hold).

    ``hold_end`` (Issue #1205, owner decision 2026-11-01) is the hold's ORIGINAL deadline when the test changed the
    thermostat after the off: the hold runs to it whatever replaced the grace timer, so it is checked through
    ``_fan_off_hold_active()``, not the running grace. ``tick`` runs once per step before the periodic checks (fires
    the scheduled timers: the override confirmation, grace expiries). ``economizer`` also drives the window-cooling
    check each step. The loop covers the hold up to its last second.
    """
    engine = home.engine
    engine._test_state.attributes["current_temperature"] = indoor
    problems: list[str] = []
    last_cancel = [d for n, d in home.events if n == "fan_cancel"][-1]
    if matched_kind is None:
        if last_cancel.get("trigger") != "fan_off":
            problems.append(f"label: expected plain fan_off, got {last_cancel.get('trigger')!r}")
    elif last_cancel.get("trigger") != "manual_origin_carryforward" or last_cancel.get("manual_origin_kind") != (
        matched_kind
    ):
        problems.append(f"label: expected manual_origin_carryforward/{matched_kind}, got {last_cancel!r}")
    if not engine._fan_off_hold_active():
        problems.append(
            f"no fan-off hold (grace_active={engine._grace_active}, trigger={engine._last_grace_trigger!r})"
        )
    assert home.window_open, "precondition: the window is open for the whole hold"
    on_cmds_before = len(_fan_on_commands(home))
    events_before = len(home.events)
    # The hold is the configured fan-off grace, measured from the off. It is driven even when no grace is running
    # (the pre-#1205 symptom), so the fan being switched back on is what fails, not only the missing grace.
    if hold_end is None:
        hold_s = float(home.engine.config.get(CONF_MANUAL_GRACE_PERIOD, DEFAULT_MANUAL_GRACE_SECONDS))
        hold_end = home.clock.now + timedelta(seconds=hold_s)
    steps = 0
    restarted = False
    last_second = hold_end - timedelta(seconds=1)
    while home.clock.now < last_second and not restarted:
        home.clock.advance(min(_HOLD_STEP_S, (last_second - home.clock.now).total_seconds()))
        if tick is not None:
            tick()
        asyncio.run(engine.check_natural_vent_conditions())
        asyncio.run(engine.nat_vent_temperature_check(indoor, outdoor=65.0))
        if economizer:
            engine._test_state.attributes["current_temperature"] = indoor
            asyncio.run(engine.check_window_cooling_opportunity(65.0, indoor, True, current_hour=18))
        home.drain()
        steps += 1
        if engine._fan_active or len(_fan_on_commands(home)) != on_cmds_before:
            problems.append(f"CA turned the fan back on {steps * _HOLD_STEP_S} s into the hold")
            restarted = True
        if any(n in ("fan_activated", "nat_vent_reactivated_while_paused") for n, _ in home.events[events_before:]):
            problems.append("a fan start was recorded during the hold")
            restarted = True
    assert steps >= 1, "the hold must be long enough to drive at least one periodic check"
    if problems:
        raise _OccupantOutcomeViolated("; ".join(problems))


# ---------------------------------------------------------------------------
# Control: the assertion itself is sound
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", _MODES)
def test_control_ordinary_start_then_occupant_off_starts_fan_off_grace(clock, fan_mode):
    """No prior arm: an ordinary CA start followed by the occupant's off starts a normal fan-off grace."""
    home = _Home(fan_mode, clock)
    home.close_window()  # house starts sealed
    _unrelated_ordinary_ca_start(home)

    home.occupant_turns_fan_off()

    assert home.cancel_triggers()[-1] == "fan_off"
    assert home.engine._last_resume_source == "manual"
    _assert_fan_held_off(home)


@pytest.mark.parametrize("fan_mode", _MODES)
def test_the_assertion_catches_a_fan_restarted_during_the_hold(clock, fan_mode):
    """The helper is not vacuous: a fan-on commanded mid-hold (here by the test) is reported as the symptom."""
    home = _Home(fan_mode, clock)
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    home.occupant_turns_fan_off()
    real_check = home.engine.check_natural_vent_conditions

    async def _restarts_the_fan() -> None:
        await real_check()
        # Issue #134/#1205: _activate_fan() now refuses every CA start inside the hold itself (the backstop), so the
        # simulated defect must look past that guard -- the control proves the ASSERTION, not the backstop.
        with patch.object(home.engine, "_fan_off_hold_active", return_value=False):
            await home.engine._activate_fan(reason="simulated restart during the hold")

    home.engine.check_natural_vent_conditions = _restarts_the_fan
    with pytest.raises(_OccupantOutcomeViolated, match="turned the fan back on"):
        _assert_fan_held_off(home)


# ---------------------------------------------------------------------------
# R1: arm A3 (rate-limited reactivation), deferral abandoned, later unrelated start
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", _MODES)
def test_r1_abandoned_rate_limited_reactivation_then_unrelated_start_off_gets_grace(clock, fan_mode):
    home = _Home(fan_mode, clock)
    engine = home.engine
    _start_automation_grace_with_window_open(home)
    _grace_expires_inside_the_toggle_floor(home)

    # the reactivation was deferred by the toggle floor: no fan, no nat-vent, still paused for the window
    assert not engine._fan_active
    assert not engine._natural_vent_active
    assert engine._paused_by_door is True
    assert "fan_activated" not in home.event_names()

    # the window closes before any retry: the deferred start is abandoned and the fan never ran
    home.close_window()
    assert not engine._fan_active
    assert "fan_activated" not in home.event_names()

    _unrelated_ordinary_ca_start(home)
    home.occupant_turns_fan_off()
    _assert_fan_held_off(home)


# ---------------------------------------------------------------------------
# R2: arm A4 (applied reactivation), CA ends the session itself, later unrelated start
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", _MODES)
def test_r2_applied_reactivation_ended_by_ca_then_unrelated_start_off_gets_grace(clock, fan_mode):
    home = _Home(fan_mode, clock)
    engine = home.engine
    _start_automation_grace_with_window_open(home)
    home.expire_grace()

    # the reactivation was applied: CA's nat-vent session is running with the window still open
    assert engine._fan_active
    assert engine._natural_vent_active
    assert "fan_activated" in home.event_names()

    # CA itself ends the session (window closes past the toggle floor): the off never reaches on_fan_turned_off()
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    home.close_window()
    assert not engine._fan_active
    assert not engine._natural_vent_active
    assert "sensor_all_closed" in home.event_names()
    assert "fan_cancel" not in home.event_names()

    _unrelated_ordinary_ca_start(home)
    home.occupant_turns_fan_off()
    _assert_fan_held_off(home)


# R3 (arm A1, ``clear_fan_override()`` with the fan still running and no RF timer) is deliberately not driven here.
# It needs a CA-owned running fan (``_fan_active``) under an occupant override whose grace expires with every sensor
# closed; a fan override with a window open expires through ``_re_pause_for_open_sensor()`` instead (that is R2,
# arm A4), and a whole-house fan in a sealed house is stopped within 30 s by the sealed-house safety (rule 1). No
# natural drive of the A1 arm that also ends with CA stopping the fan itself was found at the unit tier.


# ---------------------------------------------------------------------------
# Issue #1205 (flipped from #829): the off of the marked run IS held off, and carries the origin label
# ---------------------------------------------------------------------------


def _assert_held_with_label(home: _Home, kind: str) -> None:
    """Issue #1205: the occupant's off of the run the marker belongs to is held off like any other, and labelled."""
    _assert_fan_held_off(home, matched_kind=kind)
    assert home.engine._fan_manual_origin_unclosed is False, "one-shot: consumed"
    assert home.engine._fan_manual_origin_run is None


@pytest.mark.parametrize("fan_mode", _MODES)
def test_829_applied_reactivation_then_its_own_off_is_held_and_labelled(clock, fan_mode, caplog):
    """Arm A4: the grace-expiry reactivation runs, the occupant turns THAT run off. Issue #1205 (flipped): the
    off is a person, so the fan stays off for the whole hold; the label says CA restarted it after the grace."""
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    _start_automation_grace_with_window_open(home)
    home.expire_grace()
    assert engine._fan_active
    assert engine._fan_manual_origin_unclosed is True
    assert engine._fan_manual_origin_run == engine._fan_on_since
    # The restart at the end of a grace is visible in the Activity Record (no new event type is needed).
    assert "nat_vent_reactivated_while_paused" in home.event_names()

    home.occupant_turns_fan_off()

    _assert_held_with_label(home, "restart")
    assert "marker consumed, run matched (label only)" in caplog.text
    assert "holding fan off (fan_off grace); closes session from earlier manual fan-on" in caplog.text
    assert "kind=restart" in caplog.text and "armed_site=re_pause_for_open_sensor" in caplog.text


@pytest.mark.parametrize("fan_mode", _MODES)
def test_829_deferred_then_applied_reactivation_then_its_own_off_is_held_and_labelled(clock, fan_mode, caplog):
    """Arm A3: the reactivation is deferred (pending, nothing armed), the retry applies while the pause lasts (the
    pending is promoted to the run-bound marker), the occupant turns THAT run off. Issue #1205 (flipped): held off
    for the whole grace, labelled "restarted after your grace ended"."""
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    _start_automation_grace_with_window_open(home)
    _grace_expires_inside_the_toggle_floor(home)

    assert not engine._fan_active
    assert engine._fan_manual_origin_pending is not None, "the deferral is recorded as a pending fact"
    assert engine._fan_manual_origin_unclosed is False, "nothing runs yet, so nothing is armed (#1205)"
    assert "pending raised" in caplog.text

    # the toggle floor ends with the window still open: the next cycle's retry starts the fan
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    asyncio.run(engine.check_natural_vent_conditions())
    home.drain()
    assert engine._fan_active, "precondition: the deferred reactivation applied"
    assert engine._fan_manual_origin_pending is None
    assert engine._fan_manual_origin_unclosed is True
    assert engine._fan_manual_origin_run == engine._fan_on_since
    assert "pending promoted" in caplog.text

    home.occupant_turns_fan_off()

    _assert_held_with_label(home, "restart")


# ---------------------------------------------------------------------------
# Every stale discard is logged, and the abandoned deferral leaves nothing behind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", _MODES)
def test_abandoned_deferral_logs_pending_discard_and_leaves_no_deferral_record(clock, fan_mode, caplog):
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    _start_automation_grace_with_window_open(home)
    _grace_expires_inside_the_toggle_floor(home)
    assert engine._fan_rate_limited_direction == "activate"

    home.close_window()  # the pause ends: the deferred start is abandoned

    assert engine._fan_manual_origin_pending is None
    assert "pending discarded: reason=door_pause_ended" in caplog.text
    # the abandoned deferral record goes with it: a later unrelated start must not "apply a deferred activation"
    assert engine._fan_rate_limited_until is None
    assert engine._fan_rate_limited_direction is None
    caplog.clear()
    _unrelated_ordinary_ca_start(home)
    assert "applying deferred activation" not in caplog.text


@pytest.mark.parametrize("fan_mode", _MODES)
def test_run_mismatch_at_the_off_logs_a_stale_discard(clock, fan_mode, caplog):
    """R2: the marker was armed for a run CA stopped itself; the later run's off discards it, with an INFO line."""
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    _start_automation_grace_with_window_open(home)
    home.expire_grace()
    armed_run = engine._fan_manual_origin_run
    assert armed_run is not None
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    new_run = engine._fan_on_since
    assert new_run != armed_run
    caplog.clear()

    home.occupant_turns_fan_off()

    assert "marker discarded as stale (plain label): reason=different_run" in caplog.text
    assert f"armed_run={armed_run}" in caplog.text
    assert f"current_run={new_run}" in caplog.text
    assert engine._grace_is_fan_off_grace()


@pytest.mark.parametrize("fan_mode", _MODES)
def test_another_start_while_deferred_makes_the_pending_stale(clock, fan_mode, caplog):
    """Another fan start counted since the pending was raised (the reconcile adopt and CA's own start both bump
    ``_fan_start_seq``) means the deferred start is no longer "the" run: the later application is not promoted."""
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    _start_automation_grace_with_window_open(home)
    _grace_expires_inside_the_toggle_floor(home)
    assert engine._fan_manual_origin_pending is not None

    # another fan start is counted inside the same pause (what every start site does), then it ends again
    engine._fan_start_seq += 1

    # the toggle floor ends and the retry of the original deferral lands
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    asyncio.run(engine.check_natural_vent_conditions())
    home.drain()
    assert engine._fan_active, "precondition: the deferred activation still applied"

    assert engine._fan_manual_origin_pending is None
    assert engine._fan_manual_origin_unclosed is False
    assert "pending discarded: reason=other_start_since_raised" in caplog.text
    assert "pending promoted" not in caplog.text


@pytest.mark.parametrize("fan_mode", _MODES)
def test_a_start_that_applies_no_deferral_makes_the_pending_stale(clock, fan_mode, caplog):
    """A start that is not the recorded deferral (here: none is recorded any more) never promotes the pending."""
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    _start_automation_grace_with_window_open(home)
    _grace_expires_inside_the_toggle_floor(home)
    pending = engine._fan_manual_origin_pending
    assert pending is not None
    engine._fan_rate_limited_until = None  # the record is gone (what F3/F4 leave behind in other shapes)
    engine._fan_rate_limited_direction = None

    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    asyncio.run(engine._activate_fan(reason="unrelated start"))
    home.drain()
    assert engine._fan_active

    assert engine._fan_manual_origin_pending is None
    assert engine._fan_manual_origin_unclosed is False
    assert "pending discarded: reason=start_not_the_deferred_one" in caplog.text


@pytest.mark.parametrize("fan_mode", _MODES)
def test_occupant_off_logs_pending_discard(clock, fan_mode, caplog):
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    _start_automation_grace_with_window_open(home)
    _grace_expires_inside_the_toggle_floor(home)
    assert engine._fan_manual_origin_pending is not None

    home.occupant_turns_fan_off()

    assert engine._fan_manual_origin_pending is None
    assert "pending discarded: reason=occupant_fan_off" in caplog.text


def test_arm_without_a_live_run_arms_nothing_and_logs(clock, caplog):
    """No usable run identity: nothing is armed, so the occupant's later off gets the normal fan-off grace."""
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(FAN_MODE_WHOLE_HOUSE, clock)
    engine = home.engine
    engine._fan_override_active = True
    engine._fan_override_time = home.clock.now.isoformat()
    engine._fan_remote_timer_hours = None
    engine._fan_active = True  # running, but no run stamp (never stamped by a start)
    engine._fan_on_since = None

    engine.clear_fan_override()
    home.drain()

    assert engine._fan_manual_origin_unclosed is False
    assert engine._fan_manual_origin_run is None
    assert "marker not armed, no live run to bind it to: site=clear_fan_override" in caplog.text


def test_a_run_started_in_the_same_pause_after_an_a1_arm_does_not_match(clock):
    """Arm A1 (override expiring on a running fan) then CA stops that run: a later run never matches the marker."""
    home = _Home(FAN_MODE_WHOLE_HOUSE, clock)
    engine = home.engine
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    first_run = engine._fan_on_since
    engine._fan_override_active = True
    engine._fan_override_time = home.clock.now.isoformat()
    engine._fan_remote_timer_hours = None

    engine.clear_fan_override()
    home.drain()
    assert engine._fan_manual_origin_unclosed is True
    assert engine._fan_manual_origin_run == first_run

    # CA stops the run itself (its off never reaches on_fan_turned_off()); a later run starts and the occupant stops it
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    asyncio.run(engine._deactivate_fan(reason="test stop"))
    home.close_window()
    assert not engine._fan_active
    _unrelated_ordinary_ca_start(home)
    assert engine._fan_on_since != first_run

    home.occupant_turns_fan_off()

    _assert_fan_held_off(home)


# ---------------------------------------------------------------------------
# F1: the deferred start lands through the idle-open re-evaluation (HVAC already off, PAUSED_IDLE)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", _MODES)
def test_829_deferred_then_applied_paused_idle_off_is_held_and_labelled(clock, fan_mode, caplog):
    """The grace expires with the window open and HVAC already off (paused-idle). The 300 s floor defers the
    reactivation; the next cycle retries through ``check_natural_vent_conditions()``'s idle-open branch (not the
    paused-reactivation helper) and restores the pause flag around the apply. The occupant's off of THAT run still
    is held off for the whole grace (Issue #1205, flipped from "carries forward"), labelled as CA's restart, with no
    false "pause ended" discard on the way."""
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    engine._test_state.state = "off"  # HVAC already off (WHF suppression / blower-only)
    _start_automation_grace_with_window_open(home)
    _grace_expires_inside_the_toggle_floor(home)

    assert not engine._fan_active
    assert engine._paused_by_door is True
    assert engine._paused_with_hvac_already_off is True, "precondition: the paused-idle shape"
    assert engine._fan_manual_origin_pending is not None
    assert engine._fan_manual_origin_unclosed is False

    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    asyncio.run(engine.check_natural_vent_conditions())
    home.drain()

    assert engine._fan_active, "precondition: the deferred reactivation applied"
    assert engine._paused_by_door is True, "the idle-open branch preserves the recorded pause"
    assert engine._fan_manual_origin_pending is None
    assert engine._fan_manual_origin_unclosed is True, "the run CA reactivated is marked"
    assert engine._fan_manual_origin_run == engine._fan_on_since
    assert "pending promoted" in caplog.text
    assert "pending discarded" not in caplog.text, "no false discard from the same-handler save/restore"
    assert "reason=door_pause_ended" not in caplog.text

    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)  # well past CA's last command
    home.occupant_turns_fan_off()

    _assert_held_with_label(home, "restart")
    assert "pending discarded" not in caplog.text


@pytest.mark.parametrize("fan_mode", _MODES)
def test_same_handler_pause_restore_does_not_discard_the_pending_fact(clock, fan_mode, caplog):
    """The idle-open branch applies the nat-vent FSM state (which clears the pause flag) and restores it. When the
    retry is still rate-limited the pending fact and the deferral record must both survive that transient edge."""
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    engine._test_state.state = "off"
    _start_automation_grace_with_window_open(home)
    _grace_expires_inside_the_toggle_floor(home)
    pending = engine._fan_manual_origin_pending
    assert pending is not None and engine._paused_with_hvac_already_off is True

    # the next cycle comes before the floor ends: the retry is deferred again (duplicate) and the apply runs
    home.clock.advance(30)
    asyncio.run(engine.check_natural_vent_conditions())
    home.drain()

    assert not engine._fan_active
    assert engine._paused_by_door is True
    assert engine._fan_manual_origin_pending == pending
    assert engine._fan_rate_limited_direction == "activate"
    assert "pending discarded" not in caplog.text


# ---------------------------------------------------------------------------
# Scenario 7: a CA cycle-off/on inside a marked session starts a new run; the occupant's off is held off
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", _MODES)
def test_ca_cycle_on_inside_a_marked_session_off_is_held_with_the_plain_label(clock, fan_mode):
    """Issue #1205 scenario 7 (flipped: the old test pinned an "accepted extra grace"; now every occupant off is held).

    The marker is bound to ONE run. When CA stops a marked run itself and starts a new one (a nat-vent cycle-off/on)
    the new run has a new ``_fan_on_since``. LABEL CHOICE: plain ``fan_off`` for the person's off of the cycled-on
    run -- the fan they stopped is a nat-vent run CA started on its own cycle, not the one it took over or
    restarted, so "you turned off the fan CA restarted after your grace ended" would be wrong. The fan is held off
    for the whole grace either way; when that grace expires CA restarts once (marked) and a person's off of THAT
    run is held and carries the restart label."""
    home = _Home(fan_mode, clock)
    engine = home.engine
    _start_automation_grace_with_window_open(home)
    home.expire_grace()
    assert engine._fan_active and engine._fan_manual_origin_unclosed is True
    marked_run = engine._fan_on_since

    # CA itself cycles the session: off (never reaching on_fan_turned_off()), then on again with the window open
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    asyncio.run(engine._deactivate_fan(reason="nat-vent cycle off"))
    home.drain()
    assert not engine._fan_active
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    asyncio.run(engine._activate_fan(reason="nat-vent cycle on"))
    home.drain()
    assert engine._fan_active
    assert engine._fan_on_since != marked_run, "the cycle-on is a new run"

    home.occupant_turns_fan_off()

    _assert_fan_held_off(home)  # plain label

    # The hold ends with the window still open: CA restarts the fan ONCE (the restart is marked and recorded), and a
    # person's off of that restarted run is held again, with the restart label.
    home.expire_grace()
    assert engine._fan_active, "precondition: the expiry reactivation applied"
    assert engine._fan_manual_origin_unclosed is True
    assert "fan_activated" in home.event_names() or "nat_vent_reactivated_while_paused" in home.event_names()
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    home.occupant_turns_fan_off()
    _assert_held_with_label(home, "restart")


# ---------------------------------------------------------------------------
# A2: reconcile adopt on post_grace_expiry, then the occupant's off of THAT run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", _MODES)
def test_829_reconcile_adopt_post_grace_expiry_then_its_own_off_is_held_and_labelled(clock, fan_mode, caplog):
    """Arm A2: the fan is still running when the grace ends and reconcile adopts it as CA's nat-vent
    (``post_grace_expiry``). The occupant's off of THAT run is held off for the whole grace (Issue #1205, flipped
    from "closes the old session: no fresh grace"), labelled "took over from your earlier manual fan-on"."""
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    assert not engine._fan_active

    asyncio.run(
        engine.reconcile_fan_on_startup(
            indoor=72.0,
            outdoor=65.0,
            thermostat_fan_running=True,
            any_sensor_open=True,
            trigger="post_grace_expiry",
        )
    )
    home.drain()

    assert engine._fan_active, "precondition: the running fan was adopted"
    assert engine._natural_vent_active
    assert engine._fan_manual_origin_unclosed is True
    assert engine._fan_manual_origin_run == engine._fan_on_since
    assert "site=reconcile_adopt" in caplog.text

    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)  # beyond the expected-confirmation window
    home.occupant_turns_fan_off()

    _assert_held_with_label(home, "takeover")
    assert "marker consumed, run matched (label only)" in caplog.text
    assert "kind=takeover" in caplog.text and "armed_site=reconcile_adopt" in caplog.text


def _manual_fan_on_with_no_timer(home: _Home) -> None:
    """A person switches the fan on with no timer selected: the manual override and the default grace start."""
    home.engine.handle_fan_manual_override(fan_before="off", fan_after="on", hvac_fan=home.hvac_fan)
    home.drain()
    assert home.engine._fan_override_active
    assert home.engine._fan_remote_timer_hours is None
    assert home.engine._grace_active


def _post_grace_reconcile_runs_like_the_coordinator(home: _Home) -> None:
    """The coordinator's post-grace fan check (``_async_post_grace_fan_reconcile``): the fan is still running."""
    engine = home.engine

    def _callback() -> None:
        engine.hass.async_create_task(
            engine.reconcile_fan_on_startup(
                indoor=72.0,
                outdoor=65.0,
                thermostat_fan_running=True,
                any_sensor_open=home.window_open,
                trigger="post_grace_expiry",
            )
        )

    engine._post_grace_fan_check_callback = _callback


@pytest.mark.parametrize("fan_mode", _MODES)
def test_a1_a2_manual_no_timer_fan_on_default_grace_expires_adopt_on_then_off_is_held(clock, fan_mode, caplog):
    """Scenario 4 end to end: a person turns the fan on (no timer), the default grace expires with the fan still
    running, the post-grace reconcile keeps it as CA's own nat-vent run (``adopt-on``), then the person turns it off.
    The fan stays off for the whole hold; the label says CA took over from the earlier manual fan-on."""
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    _post_grace_reconcile_runs_like_the_coordinator(home)
    _manual_fan_on_with_no_timer(home)

    home.expire_grace()

    assert "decision=adopt-on" in caplog.text, "precondition: reconcile kept the running fan as CA's own run"
    assert engine._fan_active and engine._natural_vent_active
    assert engine._fan_manual_origin_unclosed is True

    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    home.occupant_turns_fan_off()

    _assert_held_with_label(home, "takeover")


# ---------------------------------------------------------------------------
# Scenario 8: what stays NO hold -- the #530 settle window, and CA's own reconcile turn-off
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", _MODES)
def test_selected_timer_ending_on_schedule_inside_the_settle_window_starts_no_hold(clock, fan_mode):
    """A selected remote timer (e.g. 2 h) ran its course: its grace expired and the hardware timer's off arrives a
    few seconds later, inside the 120 s settle window (#530). CA knows that run ended on schedule: NO hold, and the
    fan_cancel trigger stays ``timer_boundary_settle``. UNCHANGED by #1205."""
    home = _Home(fan_mode, clock)
    engine = home.engine
    engine.handle_fan_manual_override(
        fan_before="off", fan_after="on", remote_timer_hours=2.0, is_remote_event=True, hvac_fan=home.hvac_fan
    )
    home.drain()
    assert engine._fan_remote_timer_hours == 2.0
    home.expire_grace()
    assert engine._timer_boundary_settle_until is not None, "precondition: the settle window is armed"

    home.clock.advance(5)  # the hardware timer's off lands a few seconds after CA's software clock
    home.occupant_turns_fan_off()

    assert home.cancel_triggers()[-1] == "timer_boundary_settle"
    assert not engine._grace_is_fan_off_grace()
    assert engine._timer_boundary_settle_until is None


@pytest.mark.parametrize("fan_mode", _MODES)
def test_selected_timer_off_outside_the_settle_window_is_a_person_and_is_held(clock, fan_mode):
    """The settle window is time-boxed: an off long after the timer boundary is a person's and starts the hold."""
    home = _Home(fan_mode, clock)
    engine = home.engine
    engine._timer_boundary_settle_until = home.clock.now - timedelta(seconds=1)  # window already over
    engine._fan_active = True
    engine._fan_on_since = home.clock.now.isoformat()
    engine._natural_vent_active = True

    home.occupant_turns_fan_off()

    assert home.cancel_triggers()[-1] == "fan_off"
    assert engine._grace_is_fan_off_grace()


@pytest.mark.parametrize("fan_mode", _MODES)
def test_no_timer_default_expiry_with_reconcile_turn_off_is_cas_own_off_and_starts_no_hold(clock, fan_mode, caplog):
    """No timer selected, the default grace expires, the fan is still running but nat-vent is not warranted: the
    reconcile ``turn-off`` is CA's OWN command. It never reaches ``on_fan_turned_off()`` (no fan_cancel) and starts no
    fan_off grace. UNCHANGED by #1205: the hold is only for a fan-off CA did not command."""
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    engine._last_outdoor_temp = 90.0  # outdoor warmer than indoor: nat-vent not warranted
    home.window_open = True
    _post_grace_reconcile_runs_like_the_coordinator(home)
    _manual_fan_on_with_no_timer(home)
    engine._post_grace_fan_check_callback = lambda: engine.hass.async_create_task(
        engine.reconcile_fan_on_startup(
            indoor=72.0,
            outdoor=90.0,
            thermostat_fan_running=True,
            any_sensor_open=True,
            trigger="post_grace_expiry",
        )
    )

    home.expire_grace()

    assert "decision=turn-off" in caplog.text, "precondition: the reconcile turned the unwarranted fan off itself"
    assert "fan_cancel" not in home.event_names()
    assert not engine._grace_is_fan_off_grace()
    assert engine._fan_manual_origin_unclosed is False


# ---------------------------------------------------------------------------
# Issue #134 x #1205: the hot-house exception is for CA's own graces, never the occupant's fan-off hold
# ---------------------------------------------------------------------------
#
# Occupant story: the house is above comfort_cool (74), a window is open and it is cooler outside. The person
# switches the fan off. Before this change CA switched it back on at the next check (~240 s later) because #134
# ("ignore any grace when indoor > comfort_cool") also overrode the person's hold.
#
# hvac_fan note: in hvac_fan mode the reactivation GATE itself refuses above the ceiling (comfort_cool, or
# comfort_cool + 2 F with aggressive_savings: nat_vent_gate._resolve_ceiling_threshold), so the blower did not
# restart in the default config -- the compressor owns that case (#392). With aggressive_savings, a house inside
# (comfort_cool, comfort_cool + 2] passes the gate and the blower WOULD restart: that row is the hvac_fan proof.

_HOT_CASES = [
    pytest.param(FAN_MODE_WHOLE_HOUSE, {}, 78.0, id="whf-78F"),
    pytest.param(FAN_MODE_HVAC, {}, 78.0, id="hvac_fan-78F-gate-refuses-above-ceiling-anyway"),
    pytest.param(FAN_MODE_HVAC, {"aggressive_savings": True}, 75.5, id="hvac_fan-savings-75.5F-gate-would-pass"),
]


@pytest.mark.parametrize(("fan_mode", "cfg", "hot"), _HOT_CASES)
def test_134_hot_house_does_not_override_the_occupants_fan_off_hold(clock, fan_mode, cfg, hot, caplog):
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock, **cfg)
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    home.occupant_turns_fan_off()
    assert hot > home.engine.config["comfort_cool"], "precondition: the house is above the comfort ceiling"

    _assert_fan_held_off(home, indoor=hot)

    assert home.engine._grace_is_fan_off_grace()
    # one INFO line per hold names the outcome first, then the key=value facts
    lines = [
        r.getMessage() for r in caplog.records if "occupant's fan-off hold wins over comfort-ceiling" in r.getMessage()
    ]
    if not (fan_mode == FAN_MODE_HVAC and not cfg):
        assert len(lines) == 1, lines
        assert lines[0].startswith("Nat-vent not re-entered: occupant's fan-off hold wins over comfort-ceiling bypass")
        assert "indoor=" in lines[0] and "comfort_cool=" in lines[0] and "grace_remaining_min=" in lines[0]


@pytest.mark.parametrize(("fan_mode", "cfg", "hot"), _HOT_CASES)
def test_134_preserved_ca_own_automation_grace_still_lets_a_hot_house_back_in(clock, fan_mode, cfg, hot):
    """CA stopped the fan itself (comfort floor / nat-vent exit resume): that is an automation grace, NOT the
    occupant's hold, so the #134 hot-house exception still re-enters nat-vent (where the gate allows it)."""
    home = _Home(fan_mode, clock, **cfg)
    engine = home.engine
    _start_automation_grace_with_window_open(home)
    assert engine._last_grace_trigger != "fan_off" and not engine._grace_is_fan_off_grace()
    engine._test_state.attributes["current_temperature"] = hot
    assert not engine._fan_active

    asyncio.run(engine.check_natural_vent_conditions())
    home.drain()

    if fan_mode == FAN_MODE_HVAC and not cfg:
        # the gate's own ceiling (#392) refuses an hvac_fan restart above comfort_cool: the bypass is not the reason
        assert not engine._fan_active
    else:
        assert engine._fan_active, "#134: a hot house must still re-enter nat-vent during CA's own grace"
        assert engine._natural_vent_active


def test_134_the_hold_ends_with_the_grace_and_the_hot_house_then_re_enters(clock):
    """The hold is not permanent: once the fan-off grace is over the ordinary reactivation applies again."""
    home = _Home(FAN_MODE_WHOLE_HOUSE, clock)
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    home.occupant_turns_fan_off()
    engine = home.engine
    engine._test_state.attributes["current_temperature"] = 78.0
    assert engine._grace_is_fan_off_grace()
    home.expire_grace()

    assert not engine._grace_is_fan_off_grace()
    assert engine._fan_active or engine._paused_by_door, "after the hold the reactivation (or its pause) resumes"


def test_activate_fan_backstop_refuses_every_ca_start_during_the_hold_but_not_after(clock, caplog):
    """Defense in depth: _activate_fan() itself refuses a CA start while the occupant's hold runs (the economizer's
    window-cooling start had no grace check at all). SUPPRESSED = "no working fan": no session is recorded."""
    from custom_components.climate_advisor.automation import FanCommandResult

    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(FAN_MODE_WHOLE_HOUSE, clock)
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    home.occupant_turns_fan_off()
    engine = home.engine
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)  # the toggle floor is not the reason
    assert engine._grace_is_fan_off_grace()
    calls_before = len(_fan_on_commands(home))

    result = asyncio.run(engine._activate_fan(reason="economizer maintain — fan assists ventilation"))

    assert result is FanCommandResult.SUPPRESSED
    assert not engine._fan_active and len(_fan_on_commands(home)) == calls_before
    assert "Fan activation refused: occupant's fan-off hold active reason=economizer maintain" in caplog.text
    # CA's own graces never block a start through this guard: once the hold's own deadline has passed, only the
    # automation grace is left, and it does not stop the start
    home.clock.advance((engine._fan_off_hold_until - home.clock.now).total_seconds() + 1)
    assert not engine._fan_off_hold_active()
    engine._grace_active = False
    engine._start_grace_period("automation", "nat_vent_exit_resume")
    assert not engine._grace_is_fan_off_grace()
    home.open_window()
    result = asyncio.run(engine._activate_fan(reason="after the hold"))
    assert result in (FanCommandResult.EXECUTED, FanCommandResult.ALREADY_IN_STATE)


# ---------------------------------------------------------------------------
# Issue #1205 (mirror, label only): the occupant taking the fan over clears the run-bound marker; a re-arm re-labels
# ---------------------------------------------------------------------------


def _ca_restarted_run(home: _Home) -> None:
    """CA restarts the fan after a CA grace ended with the window open: an armed 'restart' marker on a live run."""
    engine = home.engine
    _start_automation_grace_with_window_open(home)
    home.expire_grace()
    assert engine._fan_active
    assert engine._fan_manual_origin_unclosed is True and engine._fan_manual_origin_kind == "restart"


@pytest.mark.parametrize("fan_mode", _MODES)
def test_person_presses_fan_on_again_on_a_restarted_run_clears_the_marker_and_the_off_gets_the_plain_label(
    clock, fan_mode, caplog
):
    """The person presses 'on' on the run CA restarted, then switches it off: it is THEIR run, so the Activity Record
    must not say CA 'restarted after your grace ended'. The off still starts the hold."""
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    _ca_restarted_run(home)

    engine.handle_fan_manual_override(fan_before="on", fan_after="on", hvac_fan=home.hvac_fan)
    home.drain()

    assert engine._fan_manual_origin_unclosed is False
    assert engine._fan_manual_origin_run is None and engine._fan_manual_origin_kind is None
    assert "Fan manual-origin marker cleared: reason=occupant_fan_override kind=restart" in caplog.text

    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    home.occupant_turns_fan_off()

    assert home.cancel_triggers()[-1] == "fan_off"
    _assert_fan_held_off(home)


@pytest.mark.parametrize("fan_mode", _MODES)
def test_person_selecting_a_remote_timer_on_a_restarted_run_clears_the_marker(clock, fan_mode, caplog):
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    _ca_restarted_run(home)

    engine.handle_fan_manual_override(
        fan_before="on", fan_after="on", remote_timer_hours=2.0, is_remote_event=True, hvac_fan=home.hvac_fan
    )
    home.drain()

    assert engine._fan_manual_origin_unclosed is False
    assert "Fan manual-origin marker cleared: reason=occupant_fan_override" in caplog.text


@pytest.mark.parametrize("fan_mode", _MODES)
def test_clearing_with_no_armed_marker_is_silent(clock, fan_mode, caplog):
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    home.engine.handle_fan_manual_override(fan_before="off", fan_after="on", hvac_fan=home.hvac_fan)
    home.drain()
    assert "marker cleared" not in caplog.text


@pytest.mark.parametrize("fan_mode", _MODES)
def test_a_rearm_for_the_same_run_overwrites_the_kind_and_site(clock, fan_mode, caplog):
    """clear_fan_override()'s re-arm ('takeover') for a run already armed as a 'restart' relabels it: the newest
    arm owns the wording. The same kind/site again stays a quiet no-op."""
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    _ca_restarted_run(home)
    run = engine._fan_manual_origin_run

    assert engine._arm_fan_manual_origin("clear_fan_override", "override_expired_while_running", kind="takeover")

    assert engine._fan_manual_origin_unclosed is True and engine._fan_manual_origin_run == run
    assert engine._fan_manual_origin_kind == "takeover"
    assert engine._fan_manual_origin_site == "clear_fan_override"
    assert "re-armed for the same run (label only): site=re_pause_for_open_sensor->clear_fan_override" in caplog.text

    caplog.clear()
    assert engine._arm_fan_manual_origin("clear_fan_override", "again", kind="takeover")
    assert "re-armed" not in caplog.text
    assert engine._fan_manual_origin_kind == "takeover"

    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    home.occupant_turns_fan_off()
    _assert_held_with_label(home, "takeover")


@pytest.mark.parametrize("fan_mode", _MODES)
def test_min_runtime_cycle_waits_out_the_occupants_hold_and_retries_when_it_ends(clock, fan_mode, caplog):
    """The person switches the fan off; the min-runtime cycle (restarted by that off) must not switch it back on
    inside the hold. It sets no 'cycle running' flag and schedules its retry for just after the hold ends, so the
    hour's minimum runtime is deferred, not lost."""
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock, fan_min_runtime_per_hour=10)
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    home.occupant_turns_fan_off()
    engine = home.engine
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)  # the toggle floor is not the reason
    assert engine._grace_is_fan_off_grace()
    calls_before = len(_fan_on_commands(home))
    remaining = (datetime.fromisoformat(engine._grace_end_time) - home.clock.now).total_seconds()

    with patch(f"{_AUTOMATION}.async_call_later") as later:
        asyncio.run(engine._fan_cycle_on())

    assert len(_fan_on_commands(home)) == calls_before and not engine._fan_active
    assert engine._fan_min_runtime_active is False
    assert "Min-runtime cycle deferred: occupant's fan-off hold active" in caplog.text
    assert later.call_args.args[1] == pytest.approx(max(30.0, remaining + 5.0))


# ---------------------------------------------------------------------------
# Issue #1205 follow-up (S1): the economizer through the occupant's hold
# ---------------------------------------------------------------------------
#
# Occupant story: a window is open on a hot day and the person switches the fan off. During the hold the economizer
# wants to ventilate, and _activate_fan() correctly refuses. The status must not claim "ventilation holding" while the
# fan is off, and once the hold is over the economizer must start the fan on its own next tick (before this fix it
# recorded itself active, saw no change on later ticks and never retried).

_ECONOMIZER_CASES = [
    pytest.param(FAN_MODE_WHOLE_HOUSE, 70.0, 73.5, "maintain", id="whf-maintain"),
    pytest.param(FAN_MODE_WHOLE_HOUSE, 73.0, 80.0, "cool-down", id="whf-cool-down"),
    pytest.param(FAN_MODE_HVAC, 70.0, 73.5, "maintain", id="hvac_fan-maintain"),
    pytest.param(FAN_MODE_HVAC, 73.0, 80.0, "cool-down", id="hvac_fan-cool-down"),
]


def _economizer_tick(home: _Home, outdoor: float, indoor: float) -> bool:
    home.engine._test_state.attributes["current_temperature"] = indoor
    result = asyncio.run(home.engine.check_window_cooling_opportunity(outdoor, indoor, True, current_hour=18))
    home.drain()
    return result


@pytest.mark.parametrize(("fan_mode", "outdoor", "indoor", "phase"), _ECONOMIZER_CASES)
def test_s1_economizer_stays_inactive_through_the_hold_then_starts_the_fan(
    clock, fan_mode, outdoor, indoor, phase, caplog
):
    caplog.set_level("DEBUG", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    home.occupant_turns_fan_off()
    assert engine._grace_is_fan_off_grace()
    # the day is hot from here on: nat-vent is not the path under test, the economizer is
    engine._current_classification = _classification("hot")
    engine._natural_vent_active = False
    caplog.clear()
    events_before = len(home.events)
    on_cmds_before = len(_fan_on_commands(home))

    for _ in range(3):
        home.clock.advance(_HOLD_STEP_S)
        assert engine._grace_is_fan_off_grace(), "precondition: still inside the hold"
        _economizer_tick(home, outdoor, indoor)
        assert not engine._fan_active and len(_fan_on_commands(home)) == on_cmds_before
        assert engine._economizer_active is False
        assert engine._economizer_phase == "inactive"
    assert "ventilation holding" not in caplog.text
    assert "Economizer phase=" not in caplog.text
    refusals = [r for r in caplog.records if "Fan activation refused: occupant's fan-off hold" in r.getMessage()]
    assert [r.levelname for r in refusals] == ["INFO", "DEBUG", "DEBUG"], "one INFO per hold per caller (N1)"

    # the hold ends: expire it without nat-vent restarting the fan on its own (the economizer is under test)
    home.clock.advance(max(0.0, (datetime.fromisoformat(engine._grace_end_time) - home.clock.now).total_seconds()))
    engine._grace_active = False
    engine._last_grace_trigger = None
    engine._grace_end_time = None
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    assert not engine._fan_active

    _economizer_tick(home, outdoor, indoor)

    assert engine._fan_active, "the economizer starts the fan on its next tick once the hold is over"
    assert "fan_activated" in home.event_names()[events_before:]
    assert engine._economizer_active is True and engine._economizer_phase == phase


# ---------------------------------------------------------------------------
# Issue #1205 follow-up (N1, N3): once-per-caller refusal log; a stale marker does not suppress the "restart" label
# ---------------------------------------------------------------------------


def test_n1_hold_refusal_logs_info_once_per_caller_per_hold(clock, caplog):
    """The reason embeds live temperatures and callers alternate every tick; the refusal still logs INFO once per
    caller per hold (DEBUG after), and a NEW hold logs INFO again."""
    from custom_components.climate_advisor.automation import FanCommandResult

    caplog.set_level("DEBUG", logger="custom_components.climate_advisor.automation")
    home = _Home(FAN_MODE_WHOLE_HOUSE, clock)
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    home.occupant_turns_fan_off()
    engine = home.engine
    assert engine._grace_is_fan_off_grace()
    caplog.clear()

    for i in range(3):  # temperatures change every tick, callers alternate
        for reason in (
            f"economizer maintain — fan assists ventilation indoor {73 + i}",
            f"natural ventilation: outdoor {65 + i}°F < indoor 72.0°F",
        ):
            assert asyncio.run(engine._activate_fan(reason=reason)) is FanCommandResult.SUPPRESSED

    def levels() -> list[str]:
        return [r.levelname for r in caplog.records if "Fan activation refused: occupant's fan-off hold" in r.message]

    assert levels() == ["INFO", "INFO", "DEBUG", "DEBUG", "DEBUG", "DEBUG"]
    assert caplog.records[0].getMessage().startswith("Fan activation refused: occupant's fan-off hold active reason=")

    # a different hold (the person switches the fan off again) logs INFO once more for the same caller
    home.expire_grace()
    engine._fan_active = False
    engine._start_grace_period("manual", "fan_off")
    engine._grace_end_time = (home.clock.now + timedelta(seconds=999)).isoformat()
    caplog.clear()
    asyncio.run(engine._activate_fan(reason="economizer maintain — fan assists ventilation indoor 73"))
    assert levels() == ["INFO"]


@pytest.mark.parametrize("fan_mode", _MODES)
def test_n3_stale_marker_from_an_earlier_run_does_not_suppress_the_restart_label(clock, fan_mode):
    """A marker armed for an EARLIER run (CA stopped that run itself) is stale. CA then restarts the fan after a
    grace ended on a run another CA path already started (ALREADY_IN_STATE): that is a CA restart, so the label
    is "restart", not left on the stale marker's kind."""
    home = _Home(fan_mode, clock)
    engine = home.engine
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    first_run = engine._fan_on_since
    engine._arm_fan_manual_origin("test_site", "earlier_run", kind="takeover")
    assert engine._fan_manual_origin_run == first_run

    # CA stops that run itself; its marker is left behind and a later run (not marked) is started by CA
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    asyncio.run(engine._deactivate_fan(reason="test stop"))
    home.close_window()
    assert not engine._fan_active
    _unrelated_ordinary_ca_start(home)
    assert engine._fan_on_since != first_run
    assert engine._fan_manual_origin_unclosed is True and engine._fan_manual_origin_run == first_run, (
        "precondition: a stale marker is still armed for the earlier run"
    )

    _start_automation_grace_with_window_open(home)
    home.expire_grace()

    assert engine._fan_manual_origin_unclosed is True
    assert engine._fan_manual_origin_run == engine._fan_on_since, "the marker now belongs to the current run"
    assert engine._fan_manual_origin_kind == "restart"


# ---------------------------------------------------------------------------
# Issue #1205 (owner decision 2026-11-01): the hold lasts until ITS OWN end, whatever the occupant does to the
# thermostat afterwards
# ---------------------------------------------------------------------------
#
# Occupant story: the person switches the fan off, then sets the thermostat to Off (or cool/heat). About ten minutes
# later the thermostat change is confirmed as an override and its grace REPLACES the fan-off grace. "Fan off" and
# "thermostat X" are separate requests: the fan must stay off for the whole pause they asked for. The hold has its own
# deadline (``_fan_off_hold_until``); every check reads ``_fan_off_hold_active()``.


class _Sched:
    """A virtual ``async_call_later``: timers fire when the test clock passes them (the real engine's timers)."""

    def __init__(self, clk: _Clock) -> None:
        self.clk = clk
        self.items: list[list] = []

    def later(self, _hass, delay, action):
        secs = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
        item = [self.clk.now + timedelta(seconds=secs), action, False]
        self.items.append(item)

        def cancel() -> None:
            item[2] = True

        return cancel

    def pending(self) -> list[float]:
        """Seconds until each live timer is due."""
        return sorted((i[0] - self.clk.now).total_seconds() for i in self.items if not i[2])

    def fire_due(self, home: _Home) -> None:
        due = sorted((i for i in self.items if not i[2] and i[0] <= self.clk.now), key=lambda i: i[0])
        for item in due:
            self.items.remove(item)
            result = item[1](self.clk.now)
            if asyncio.iscoroutine(result):
                asyncio.run(result)
            home.drain()


@pytest.fixture
def sched(clock):
    sch = _Sched(clock)
    with (
        patch(f"{_AUTOMATION}.async_call_later", side_effect=sch.later),
        patch(f"{_AUTOMATION}.callback", side_effect=lambda fn: fn),
    ):
        yield sch


def _set_thermostat(home: _Home, new_mode: str) -> None:
    """The occupant changes the thermostat; the real engine starts its confirmation window."""
    from custom_components.climate_advisor.override_grace_fsm import OverrideGraceFsmEventKind

    engine = home.engine
    old = engine._test_state.state
    engine._test_state.state = new_mode
    engine.start_override_confirmation(
        "normal", event_kind=OverrideGraceFsmEventKind.OVERRIDE_DETECTED, old_mode=old, new_mode=new_mode
    )
    home.drain()


def _fan_off_then_thermostat(home: _Home, sch: _Sched, day: str, tstat: str) -> datetime:
    """CA runs the fan, the day turns ``day``, the occupant switches the fan off and 60 s later sets the thermostat.

    Returns the hold's ORIGINAL deadline.
    """
    engine = home.engine
    engine._test_state.state = "cool" if day == "hot" else "off"
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    engine._current_classification = _classification(day)
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    home.occupant_turns_fan_off()
    assert engine._grace_is_fan_off_grace(), "precondition: the off started the fan-off grace"
    hold_end = engine._fan_off_hold_until
    assert hold_end is not None and hold_end.isoformat() == engine._grace_end_time
    home.clock.advance(60)
    _set_thermostat(home, tstat)
    return hold_end


# (day, thermostat the occupant sets). The thermostat starts at the day's own mode (hot: cool, mild: off), so each case
# is a real change except hot+cool, which agrees with the classification and is never confirmed as an override
# (the control: the fan-off grace itself runs the hold to its end). A confirmed heat/cool override is the occupant's
# own HVAC choice, which also keeps CA from starting the fan after the hold (Issue #714) -- a separate rule.
_THERMOSTAT_CASES = [
    pytest.param("hot", "off", True, id="hot-off"),
    pytest.param("hot", "heat", True, id="hot-heat"),
    pytest.param("hot", "cool", False, id="hot-cool-no-change"),
    pytest.param("mild", "cool", True, id="mild-cool"),
    pytest.param("mild", "heat", True, id="mild-heat"),
]


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize(("day", "tstat", "confirms"), _THERMOSTAT_CASES)
def test_thermostat_change_after_the_off_does_not_end_the_hold(clock, sched, fan_mode, day, tstat, confirms, caplog):
    """The fan stays off until the ORIGINAL hold end even though the thermostat override replaced the grace; only
    then may CA start it (economizer / nat-vent)."""
    from custom_components.climate_advisor.automation import FanCommandResult

    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    hold_end = _fan_off_then_thermostat(home, sched, day, tstat)
    seen = {"override_grace": False}

    def tick() -> None:
        sched.fire_due(home)
        if engine._last_grace_trigger == "override_confirmed":
            seen["override_grace"] = True

    _assert_fan_held_off(home, hold_end=hold_end, tick=tick, economizer=True, indoor=76.0)

    assert seen["override_grace"] is confirms, "the thermostat override replaces the fan-off grace (or, control, not)"
    assert engine._fan_off_hold_active()  # the occupant's off is still in force in the hold's last second
    assert engine._fan_off_hold_until == hold_end
    assert engine._grace_is_fan_off_grace() is (not confirms)  # the grace timer is the override's now ...
    continues = [r for r in caplog.records if "Fan-off hold continues" in r.getMessage()]
    if confirms:  # ... and the hold continues past it, logged at INFO, outcome first
        assert len(continues) == 1 and continues[0].levelno == logging.INFO
        assert continues[0].getMessage().startswith("Fan-off hold continues past override_confirmed grace: hold_until=")
        assert f"override_mode={tstat}" in continues[0].getMessage()
    else:
        assert not continues

    # the hold's last second has passed: the hold no longer stops CA from starting the fan
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    assert not engine._fan_off_hold_active()
    assert "Fan-off hold ended: reason=expired" in caplog.text
    caplog.clear()
    result = asyncio.run(engine._activate_fan(reason="after the hold"))
    assert "occupant's fan-off hold" not in caplog.text
    if confirms and tstat != "off":
        # the occupant's heat/cool choice is its own rule (Issue #714): the fan stays off for THAT reason
        assert result is FanCommandResult.OVERRIDDEN
    else:
        assert result in (FanCommandResult.EXECUTED, FanCommandResult.ALREADY_IN_STATE) and engine._fan_active


@pytest.mark.parametrize("fan_mode", _MODES)
def test_without_a_thermostat_change_the_hold_also_runs_to_its_deadline(clock, sched, fan_mode):
    """Control for the cases above: the same helper with no thermostat change (the plain hold) is held to the end."""
    home = _Home(fan_mode, clock)
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    home.occupant_turns_fan_off()
    hold_end = home.engine._fan_off_hold_until

    _assert_fan_held_off(home, hold_end=hold_end, tick=lambda: sched.fire_due(home), economizer=True)

    assert home.engine._fan_off_hold_until == hold_end


@pytest.mark.parametrize("fan_mode", _MODES)
def test_hold_ends_at_its_deadline_and_the_grace_expiry_logs_it(clock, sched, fan_mode, caplog):
    """The hold expires normally with its own grace: logged once, then CA may start the fan."""
    from custom_components.climate_advisor.automation import FanCommandResult

    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    home.occupant_turns_fan_off()
    assert engine._fan_off_hold_active()

    home.expire_grace()

    assert engine._fan_off_hold_until is None and not engine._fan_off_hold_active()
    assert len([r for r in caplog.records if "Fan-off hold ended: reason=expired" in r.getMessage()]) == 1
    home.open_window()
    assert asyncio.run(engine._activate_fan(reason="after the hold")) in (
        FanCommandResult.EXECUTED,
        FanCommandResult.ALREADY_IN_STATE,
    )


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize("tstat", ["off", "heat"])
def test_occupant_fan_on_during_the_hold_ends_it(clock, sched, fan_mode, tstat, caplog):
    """The newest fan action wins: switching the fan back on ends the hold (also when a thermostat override had
    replaced the grace), their fan runs, and CA's later control works once their override ends."""
    from custom_components.climate_advisor.automation import FanCommandResult

    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    hold_end = _fan_off_then_thermostat(home, sched, "hot", tstat)
    for _ in range(3):  # the override is confirmed and has replaced the grace
        home.clock.advance(_HOLD_STEP_S)
        sched.fire_due(home)
    assert engine._last_grace_trigger == "override_confirmed" and engine._fan_off_hold_active()

    home.clock.advance(60)
    engine.handle_fan_manual_override(fan_before="off", fan_after="on", hvac_fan=home.hvac_fan)
    home.drain()

    assert engine._fan_off_hold_until is None and not engine._fan_off_hold_active()
    assert "Fan-off hold ended: reason=occupant_fan_on" in caplog.text
    assert engine._fan_override_active, "the occupant's fan is theirs: CA backs off"
    assert asyncio.run(engine._activate_fan(reason="ca during the override")) is FanCommandResult.OVERRIDDEN

    # their override ends; the fan-off hold must not come back, and CA's own control works again
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    engine.clear_fan_override(reason="test_override_ended")
    home.drain()
    assert home.clock.now < hold_end, "precondition: the ORIGINAL deadline has not passed, so only the fan-on ended it"
    assert not engine._fan_off_hold_active()
    result = asyncio.run(engine._activate_fan(reason="ca control after the override"))
    if tstat == "heat" and home.hvac_fan:
        # the blower press never replaces the occupant's heat setting (Issue #1133): that rule, not the hold, applies
        assert result is FanCommandResult.OVERRIDDEN
    else:
        assert result in (FanCommandResult.EXECUTED, FanCommandResult.ALREADY_IN_STATE)


@pytest.mark.parametrize("fan_mode", _MODES)
def test_dashboard_cancel_of_the_fan_off_grace_ends_the_hold_but_cancelling_the_thermostat_override_does_not(
    clock, sched, fan_mode, caplog
):
    """Cancel on the dashboard ends the grace it cancels. The fan-off grace's cancel ends the hold; once a
    thermostat override owns the grace, cancelling THAT override leaves the separate fan-off request in force."""
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    home.occupant_turns_fan_off()
    assert engine._fan_off_hold_active()

    assert engine.cancel_override(reason="user_cancel") is True

    assert not engine._fan_off_hold_active() and engine._fan_off_hold_until is None
    assert "Fan-off hold ended: reason=grace_cancelled:user_cancel" in caplog.text

    # second hold, replaced by a thermostat override whose cancel must not end it
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    home.open_window()
    home.drain()
    engine._fan_active = True
    engine._natural_vent_active = True
    home.occupant_turns_fan_off()
    hold_end = engine._fan_off_hold_until
    assert hold_end is not None
    home.clock.advance(60)
    _set_thermostat(home, "heat")
    for _ in range(3):
        home.clock.advance(_HOLD_STEP_S)
        sched.fire_due(home)
    assert engine._last_grace_trigger == "override_confirmed"

    assert engine.cancel_override(reason="user_cancel_override") is True

    assert engine._fan_off_hold_until == hold_end and engine._fan_off_hold_active()


@pytest.mark.parametrize("fan_mode", _MODES)
def test_a_newer_fan_off_restarts_the_hold_with_a_new_deadline(clock, sched, fan_mode):
    home = _Home(fan_mode, clock)
    engine = home.engine
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    home.occupant_turns_fan_off()
    first = engine._fan_off_hold_until
    home.clock.advance(600)

    engine.on_fan_turned_off(fan_before="on", fan_after="off", hvac_fan=home.hvac_fan)
    home.drain()

    assert engine._fan_off_hold_until == first + timedelta(seconds=600)


@pytest.mark.parametrize("fan_mode", _MODES)
def test_min_runtime_cycle_retries_just_after_the_hold_even_when_the_override_replaced_the_grace(
    clock, sched, fan_mode, caplog
):
    """The deferral and its retry key on the hold's own deadline (grace timer replaced by the override's)."""
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock, fan_min_runtime_per_hour=10)
    engine = home.engine
    hold_end = _fan_off_then_thermostat(home, sched, "hot", "off")
    for _ in range(3):
        home.clock.advance(_HOLD_STEP_S)
        sched.fire_due(home)
    assert engine._last_grace_trigger == "override_confirmed" and not engine._grace_is_fan_off_grace()
    calls_before = len(_fan_on_commands(home))
    remaining = (hold_end - home.clock.now).total_seconds()
    assert remaining > 60

    with patch(f"{_AUTOMATION}.async_call_later") as later:
        asyncio.run(engine._fan_cycle_on())

    assert len(_fan_on_commands(home)) == calls_before and not engine._fan_active
    assert engine._fan_min_runtime_active is False
    assert "Min-runtime cycle deferred: occupant's fan-off hold active" in caplog.text
    assert later.call_args.args[1] == pytest.approx(remaining + 5.0)


# ---------------------------------------------------------------------------
# Persistence: the hold has its own deadline in the persisted state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", _MODES)
def test_hold_survives_a_restart_after_a_thermostat_override_replaced_its_grace(clock, sched, fan_mode, caplog):
    """HA restarts while the override's grace (not restored) is the persisted one: the hold is restored from its own
    persisted deadline and CA still never starts the fan inside it. The fan-off grace itself is restored as before."""
    from custom_components.climate_advisor.automation import FanCommandResult

    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    hold_end = _fan_off_then_thermostat(home, sched, "hot", "off")
    for _ in range(3):
        home.clock.advance(_HOLD_STEP_S)
        sched.fire_due(home)
    assert engine._last_grace_trigger == "override_confirmed"
    state = engine.get_serializable_state()
    assert state["fan_off_hold_until"] == hold_end.isoformat()

    restarted = _Home(fan_mode, clock).engine
    restarted.restore_state(state)

    assert restarted._fan_off_hold_until == hold_end and restarted._fan_off_hold_active()
    assert "Fan-off hold restored across restart" in caplog.text
    assert asyncio.run(restarted._activate_fan(reason="economizer maintain")) is FanCommandResult.SUPPRESSED

    # an expired persisted hold is not restored, and a fan-off grace that is the persisted one restores both
    clock.advance(3600)
    late = _Home(fan_mode, clock).engine
    late.restore_state(state)
    assert late._fan_off_hold_until is None


@pytest.mark.parametrize("fan_mode", _MODES)
def test_restart_with_the_fan_off_grace_itself_persisted_restores_the_hold_through_the_grace(clock, sched, fan_mode):
    home = _Home(fan_mode, clock)
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    home.occupant_turns_fan_off()
    state = home.engine.get_serializable_state()
    deadline = home.engine._fan_off_hold_until
    home.clock.advance(120)

    restarted = _Home(fan_mode, clock).engine
    restarted.restore_state(state)

    assert restarted._grace_is_fan_off_grace()
    assert restarted._fan_off_hold_until is not None
    assert abs((restarted._fan_off_hold_until - deadline).total_seconds()) < 1.0


# ---------------------------------------------------------------------------
# Cleanup C-b: a refused start does not make the min-runtime cycle "running"
# ---------------------------------------------------------------------------


def test_c_b_refused_min_runtime_start_sets_no_running_flag_and_retries_next_hour(clock, sched, caplog):
    """hvac_fan thermostat offering no usable 'on' mode: the start is SUPPRESSED. The cycle is not running (flag
    stays False, no off phase is scheduled for a fan that never started) and the start is retried next hour."""
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(FAN_MODE_HVAC, clock, fan_min_runtime_per_hour=10)
    engine = home.engine
    engine._test_state.attributes["fan_modes"] = ["auto"]  # no 'on' value, no named speed tier
    assert not engine._fan_active

    with patch(f"{_AUTOMATION}.async_call_later") as later:
        asyncio.run(engine._fan_cycle_on())

    assert engine._fan_min_runtime_active is False and not engine._fan_active
    assert "Min-runtime cycle start not applied: result=suppressed (retry in 3600s)" in caplog.text
    assert later.call_count == 1 and later.call_args.args[1] == 3600.0
    # the retry re-enters the cycle
    scheduled = later.call_args.args[2]
    with patch.object(engine, "_fan_cycle_on", new=MagicMock(return_value="cycle")) as cycle:
        scheduled(clock.now)
    cycle.assert_called_once_with()


@pytest.mark.parametrize("min_runtime", [10, 60])
def test_c_b_applied_min_runtime_start_still_sets_the_flag_and_schedules_the_off_phase(clock, sched, min_runtime):
    """The unchanged half: an applied start sets the flag; a partial hour schedules the off phase, always-on none."""
    home = _Home(FAN_MODE_HVAC, clock, fan_min_runtime_per_hour=min_runtime)
    engine = home.engine
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)

    with patch(f"{_AUTOMATION}.async_call_later") as later:
        asyncio.run(engine._fan_cycle_on())
    delays = [c.args[1] for c in later.call_args_list]

    assert engine._fan_min_runtime_active is True
    if min_runtime < 60:
        assert min_runtime * 60.0 in delays, delays  # the off phase
    else:
        assert min_runtime * 60.0 not in delays and 3600.0 not in delays, delays  # always-on: no off phase, no retry


# ---------------------------------------------------------------------------
# Issue #1205 follow-up (F1/F2): ending a THERMOSTAT override never ends the fan-off hold
# ---------------------------------------------------------------------------
# "Fan off" and "thermostat X" are separate requests. Only the occupant's fan-on, a dashboard cancel that targets the
# FAN-OFF pause, and a WHF safety stop end the hold early. The dashboard has two cancel endpoints
# (api.py: POST cancel_override -> "user_cancel_override"; POST cancel_fan_override -> "user_cancel_fan_override");
# both call AutomationEngine.cancel_override(), which cancels the running grace. A cancel targets the thermostat change
# when one is active or pending (unless it comes from the fan card), otherwise the fan-off pause.


def _cancel(home: _Home, reason: str) -> bool:
    """The dashboard's cancel (AutomationEngine.cancel_override), then the follow-ups the engine scheduled."""
    result = home.engine.cancel_override(reason=reason)
    home.drain()
    return result


def _confirm_override(home: _Home, sch: _Sched) -> None:
    for _ in range(3):
        home.clock.advance(_HOLD_STEP_S)
        sch.fire_due(home)


def _hold_after_override_then_fan_off(home: _Home, sch: _Sched, tstat: str = "off") -> datetime:
    """Order B: CA runs the fan, the occupant's thermostat change is confirmed, THEN they switch the fan off."""
    engine = home.engine
    engine._test_state.state = "cool"
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    engine._current_classification = _classification("hot")
    home.clock.advance(60)
    _set_thermostat(home, tstat)
    _confirm_override(home, sch)
    assert engine._manual_override_active and engine._last_grace_trigger == "override_confirmed"
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    home.occupant_turns_fan_off()
    assert engine._grace_is_fan_off_grace() and engine._manual_override_active
    return engine._fan_off_hold_until


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize("reason", ["user_cancel_override", "adopted_matching_decision"])
def test_f1_f2_override_confirmed_then_fan_off_then_thermostat_cancel_or_adoption_keeps_the_hold(
    clock, sched, fan_mode, reason, caplog
):
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    hold_end = _hold_after_override_then_fan_off(home, sched)

    assert _cancel(home, reason) is True

    assert not engine._manual_override_active
    # the occupant's outcome first: the fan stays off for the whole original hold ...
    _assert_fan_held_off(home, hold_end=hold_end, tick=lambda: sched.fire_due(home), economizer=True, indoor=76.0)
    # ... and the hold itself was never ended
    assert engine._fan_off_hold_until == hold_end and engine._fan_off_hold_active()
    assert "Fan-off hold ended" not in caplog.text


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize("pending", [True, False], ids=["pending-confirmation", "confirmed-override"])
def test_f2_fan_off_then_thermostat_change_then_thermostat_cancel_keeps_the_hold(
    clock, sched, fan_mode, pending, caplog
):
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    hold_end = _fan_off_then_thermostat(home, sched, "hot", "off")
    if not pending:
        _confirm_override(home, sched)
        assert engine._manual_override_active and engine._last_grace_trigger == "override_confirmed"
    else:
        assert engine._override_confirm_pending

    assert _cancel(home, "user_cancel_override") is True

    # the occupant's outcome first: the fan stays off for the whole original hold ...
    _assert_fan_held_off(home, hold_end=hold_end, tick=lambda: sched.fire_due(home), economizer=True, indoor=76.0)
    # ... and the hold itself was never ended
    assert engine._fan_off_hold_until == hold_end and engine._fan_off_hold_active()
    assert "Fan-off hold ended" not in caplog.text


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize("reason", ["user_cancel_override", "user_cancel_fan_override"])
def test_f2_dashboard_cancel_of_the_fan_off_pause_itself_still_ends_the_hold(clock, sched, fan_mode, reason, caplog):
    """No thermostat change behind it: either dashboard cancel targets the fan-off pause and ends the hold."""
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    home.occupant_turns_fan_off()
    assert engine._fan_off_hold_active()

    assert _cancel(home, reason) is True

    assert engine._fan_off_hold_until is None
    assert f"Fan-off hold ended: reason=grace_cancelled:{reason}" in caplog.text


@pytest.mark.parametrize("fan_mode", _MODES)
def test_f2_fan_card_cancel_ends_the_hold_even_with_a_thermostat_change_behind_it(clock, sched, fan_mode):
    """The fan card's own cancel (user_cancel_fan_override) targets the fan side."""
    home = _Home(fan_mode, clock)
    engine = home.engine
    _hold_after_override_then_fan_off(home, sched)

    assert _cancel(home, "user_cancel_fan_override") is True

    assert engine._fan_off_hold_until is None


# ---------------------------------------------------------------------------
# Issue #1205 follow-up (F3): the occupant's fan run keeps its OWN deadline past a thermostat override's grace
# ---------------------------------------------------------------------------

_RUN_S = 4 * 3600.0


def _fan_off_commands(home: _Home) -> list:
    """Every command that stops the fan (whole-house fan turn_off, blower set_fan_mode auto)."""
    out = []
    for c in home.engine.hass.services.async_call.await_args_list:
        domain, service, data = c.args[0], c.args[1], c.args[2] if len(c.args) > 2 else {}
        if (service == "turn_off" and domain != "climate") or (
            service == "set_fan_mode" and data.get("fan_mode") == "auto"
        ):
            out.append(c)
    return out


def _timer_run_then_thermostat_off(home: _Home, sch: _Sched):
    """The occupant starts the fan with a 4 h remote timer, then sets the thermostat Off (confirmed 10 min later)."""
    engine = home.engine
    engine._test_state.state = "cool"
    home.window_open = True
    engine.handle_fan_manual_override(
        fan_before="off",
        fan_after="on",
        duration_override=_RUN_S,
        remote_timer_hours=4.0,
        is_remote_event=True,
        hvac_fan=home.hvac_fan,
    )
    home.drain()
    run_end = engine._fan_override_until
    assert run_end is not None and engine._last_grace_trigger == "fan_manual_override"
    home.clock.advance(60)
    _set_thermostat(home, "off")
    _confirm_override(home, sch)
    return run_end


@pytest.mark.parametrize("fan_mode", _MODES)
def test_f3_thermostat_override_grace_ending_does_not_cut_the_fan_run_short(clock, sched, fan_mode, caplog):
    caplog.set_level("INFO", logger="custom_components.climate_advisor.automation")
    home = _Home(fan_mode, clock)
    engine = home.engine
    run_end = _timer_run_then_thermostat_off(home, sched)
    assert engine._last_grace_trigger == "override_confirmed" and engine._fan_override_active
    stops_before = len(_fan_off_commands(home))

    for _ in range(12):  # past the 30 min override grace's expiry
        home.clock.advance(300)
        sched.fire_due(home)

    assert engine._fan_override_active, "the 4 h fan run continues past the thermostat override's grace"
    assert engine._fan_override_until == run_end
    assert not engine._manual_override_active
    assert len(_fan_off_commands(home)) == stops_before, "CA never stops the occupant's fan"
    assert "Fan override continues past thermostat override grace" in caplog.text
    assert engine._last_grace_trigger == "fan_manual_override" and engine._grace_active

    # and it ends on its OWN deadline, with the RF timer's settle window armed for the timer boundary
    home.clock.advance((run_end - home.clock.now).total_seconds() + 1)
    sched.fire_due(home)
    assert not engine._fan_override_active and engine._fan_override_until is None
    assert engine._timer_boundary_settle_until is not None


@pytest.mark.parametrize("fan_mode", _MODES)
def test_f3_thermostat_cancel_keeps_the_fan_run_but_the_fan_card_cancel_ends_it(clock, sched, fan_mode):
    home = _Home(fan_mode, clock)
    engine = home.engine
    run_end = _timer_run_then_thermostat_off(home, sched)

    assert _cancel(home, "user_cancel_override") is True
    assert engine._fan_override_active and engine._fan_override_until == run_end and not engine._manual_override_active

    assert _cancel(home, "user_cancel_fan_override") is True
    assert not engine._fan_override_active and engine._fan_override_until is None


@pytest.mark.parametrize("fan_mode", _MODES)
def test_f3_the_other_ways_a_fan_override_ends_are_unchanged(clock, sched, fan_mode):
    """Occupant fan-off, bedtime and the plain grace expiry of the fan override itself still end it."""
    home = _Home(fan_mode, clock)
    engine = home.engine
    _timer_run_then_thermostat_off(home, sched)
    engine.clear_manual_override(reason="bedtime")
    home.drain()
    assert not engine._fan_override_active and engine._fan_override_until is None

    engine.handle_fan_manual_override(fan_before="off", fan_after="on", hvac_fan=home.hvac_fan)
    home.drain()
    assert engine._fan_override_until is not None
    engine.on_fan_turned_off(fan_before="on", fan_after="off", hvac_fan=home.hvac_fan)
    home.drain()
    assert not engine._fan_override_active and engine._fan_override_until is None


@pytest.mark.parametrize("fan_mode", _MODES)
def test_f3_fan_run_deadline_survives_a_restart_while_a_thermostat_grace_is_the_persisted_one(clock, sched, fan_mode):
    home = _Home(fan_mode, clock)
    run_end = _timer_run_then_thermostat_off(home, sched)
    state = home.engine.get_serializable_state()
    assert state["fan_override_until"] == run_end.isoformat()
    assert state["last_grace_trigger"] == "override_confirmed"

    restarted = _Home(fan_mode, clock).engine
    restarted.restore_state(state)

    assert restarted._pending_override_rearm_end == run_end.isoformat()


# ---------------------------------------------------------------------------
# Issue #1205 follow-up (F6/F7): wording
# ---------------------------------------------------------------------------


def test_f7_status_names_the_fan_requests_that_outlive_the_running_grace(clock, sched):
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    home = _Home(FAN_MODE_WHOLE_HOUSE, clock)
    engine = home.engine
    hold_end = _fan_off_then_thermostat(home, sched, "hot", "off")
    _confirm_override(home, sched)
    assert engine._last_grace_trigger == "override_confirmed"
    coord = object.__new__(ClimateAdvisorCoordinator)

    detail = coord._grace_status_detail(engine)

    local = hold_end.astimezone().strftime("%I:%M %p").lstrip("0")
    assert "thermostat override" in detail
    assert f"fan held off until {local}" in detail or "fan held off until" in detail
    assert "your fan run continues" not in detail


def test_f7_status_names_the_fan_run_that_outlives_the_thermostat_grace(clock, sched):
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    home = _Home(FAN_MODE_WHOLE_HOUSE, clock)
    engine = home.engine
    _timer_run_then_thermostat_off(home, sched)
    assert engine._last_grace_trigger == "override_confirmed"
    coord = object.__new__(ClimateAdvisorCoordinator)

    detail = coord._grace_status_detail(engine)

    assert "your fan run continues until" in detail and "fan held off" not in detail


# ---------------------------------------------------------------------------
# Issue #1205 final review: the thermostat mode-to-mode change (coordinator Fix D) keeps the fan run; cancel targeting
# ---------------------------------------------------------------------------


def _fix_d(home: _Home, new_mode: str) -> None:
    """Exactly what coordinator.py's mode-to-mode path does for a different mode during a confirmed override."""
    from custom_components.climate_advisor.override_grace_fsm import OverrideGraceFsmEventKind

    engine = home.engine
    old = engine._test_state.state
    engine._test_state.state = new_mode
    engine._replace_thermostat_override(
        reason="new_override_during_grace", fsm_kind=OverrideGraceFsmEventKind.OVERRIDE_SUPERSEDED, clear_fan=False
    )
    engine.handle_manual_override(old_mode=old, new_mode=new_mode, classification_mode=None)
    home.drain()


def test_fix_d_call_site_does_not_ask_to_clear_the_fan_run():
    """The coordinator's mode-to-mode path passes clear_fan=False (the one production caller)."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "custom_components/climate_advisor/coordinator.py").read_text(
        encoding="utf-8"
    )
    assert "clear_fan=True" not in src
    assert "fsm_kind=_OGFEventKind.OVERRIDE_SUPERSEDED, clear_fan=False" in src


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize("second", ["heat", "cool"])
def test_thermostat_off_then_heat_or_cool_does_not_cut_the_fan_run_short(clock, sched, fan_mode, second):
    """A 4 h remote-timer run, thermostat Off (confirmed), then heat or cool: the run survives the replacement, is kept
    at the thermostat grace's end and ends at its own deadline with the RF settle window armed. In hvac_fan mode
    nothing conflicts at all; in whole_house_fan mode the WHF safety mutex (not a silent clear) handles heat/cool:
    rule 2's newer heat/cool stops the fan, and since Issue #1204 (owner decision A3) the run ends with that stop
    (``whf_safety_stop``) instead of staying armed, fan off, until its old deadline."""
    home = _Home(fan_mode, clock)
    engine = home.engine
    run_end = _timer_run_then_thermostat_off(home, sched)
    assert engine._manual_override_active and engine._fan_override_active
    stops_before = len(_fan_off_commands(home))
    home.clock.advance(60)

    _fix_d(home, second)

    if fan_mode == FAN_MODE_WHOLE_HOUSE:
        _confirm_override(home, sched)
        # (this harness configures no fan entity, so the stand-down's stop is EXECUTED without a service call)
        assert not engine._fan_override_active and engine._fan_override_until is None, "the stopped run has ended"
        return
    assert engine._fan_override_active and engine._fan_override_until == run_end
    assert len(_fan_off_commands(home)) == stops_before
    _confirm_override(home, sched)
    while home.clock.now < run_end - timedelta(seconds=900):
        home.clock.advance(300)
        sched.fire_due(home)
        assert engine._fan_override_active, f"the occupant's fan run was cut short at {home.clock.now}"
    home.clock.advance((run_end - home.clock.now).total_seconds() + 1)
    sched.fire_due(home)
    assert not engine._fan_override_active and engine._fan_override_until is None
    assert engine._timer_boundary_settle_until is not None


# --- cancel targeting: the fan card ends the fan side only, the thermostat cancel ends the thermostat side only ---


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize("confirmed", [True, False], ids=["confirmed-override", "pending-confirmation"])
def test_fan_card_cancel_ends_the_hold_and_leaves_the_thermostat_change_alone(clock, sched, fan_mode, confirmed):
    """Fan off, then a thermostat change (confirmed override replacing the hold's grace, or still pending), then the
    fan card's cancel: the hold ends (whichever grace is running) and the thermostat change stays."""
    home = _Home(fan_mode, clock)
    engine = home.engine
    hold_end = _fan_off_then_thermostat(home, sched, "hot", "off")
    if confirmed:
        _confirm_override(home, sched)
        assert engine._manual_override_active and engine._last_grace_trigger == "override_confirmed"
    else:
        assert engine._override_confirm_pending and engine._grace_is_fan_off_grace()

    assert _cancel(home, "user_cancel_fan_override") is True

    assert engine._fan_off_hold_until is None, "the fan card's cancel ends the fan-off pause"
    if confirmed:
        assert engine._manual_override_active and engine._last_grace_trigger == "override_confirmed"
        assert engine._grace_active, "the thermostat override keeps its grace"
    else:
        assert engine._override_confirm_pending, "a pending thermostat change is not dropped by the fan card"
    # and nothing about the thermostat change was reported as cleared
    assert not [d for n, d in home.events if n == "override_cleared" and d.get("was_mode") == "off"]
    assert hold_end is not None


@pytest.mark.parametrize("fan_mode", _MODES)
def test_thermostat_cancel_leaves_the_fan_run_and_the_hold_alone(clock, sched, fan_mode):
    """The mirror: the thermostat 'Cancel override' ends the thermostat change, not the fan run (F3) nor the hold."""
    home = _Home(fan_mode, clock)
    engine = home.engine
    run_end = _timer_run_then_thermostat_off(home, sched)

    assert _cancel(home, "user_cancel_override") is True

    assert not engine._manual_override_active
    assert engine._fan_override_active and engine._fan_override_until == run_end


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize("reason", ["user_cancel_override", "user_cancel_fan_override"])
def test_a_hold_with_no_grace_running_is_cancellable_and_the_status_names_it(clock, sched, fan_mode, reason):
    """The thermostat override's grace came and went, the hold remains with no grace at all: the Status card still says
    so and a dashboard cancel still ends it (it used to answer 'No active override or grace period')."""
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    home = _Home(fan_mode, clock)
    engine = home.engine
    hold_end = _fan_off_then_thermostat(home, sched, "hot", "off")
    _confirm_override(home, sched)
    assert _cancel(home, "user_cancel_override") is True  # the thermostat change is cancelled, the hold continues
    assert not engine._grace_active and not engine._manual_override_active
    assert engine._fan_off_hold_active() and engine._fan_off_hold_until == hold_end
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord._automation_enabled = True
    coord._startup_coalesce_active = False
    coord._occupancy_mode = "home"
    coord.automation_engine = engine
    coord._compute_automation_status_core = lambda: "active"

    assert "fan held off until" in coord._compute_automation_status()

    assert _cancel(home, reason) is True
    assert engine._fan_off_hold_until is None
    assert _cancel(home, reason) is False, "nothing left to cancel"


# --- the household-promise journal over the REAL event stream ---


def _real_episodes(home: _Home):
    from custom_components.climate_advisor.promise_journal import derive_episodes, mark_from_event

    marks = [mark_from_event(ts, name, data) for ts, name, data in home.timed_events]
    return derive_episodes([m for m in marks if m is not None])


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize("ending", ["grace_expiry", "dashboard_cancel"])
def test_journal_keeps_the_fan_run_episode_open_when_the_thermostat_override_ends(clock, sched, fan_mode, ending):
    """Real engine events: the thermostat override ends (its grace expires, or it is cancelled) while the occupant's
    fan run goes on. The run's episode must stay open until its own deadline so P4 keeps watching."""
    home = _Home(fan_mode, clock)
    run_end = _timer_run_then_thermostat_off(home, sched)
    if ending == "grace_expiry":
        for _ in range(8):
            home.clock.advance(300)
            sched.fire_due(home)
        assert home.engine._last_grace_trigger == "fan_manual_override"
    else:
        assert _cancel(home, "user_cancel_override") is True

    runs = [e for e in _real_episodes(home) if e.target == "fan" and e.kind == "override"]

    assert runs and runs[-1].end_ts is None, [(e.end_ts, e.end_reason) for e in runs]
    assert runs[-1].grace_end_ts is not None and abs((runs[-1].grace_end_ts - run_end).total_seconds()) < 5


@pytest.mark.parametrize("fan_mode", _MODES)
def test_journal_keeps_the_fan_off_episode_open_through_a_thermostat_override_and_its_cancel(clock, sched, fan_mode):
    home = _Home(fan_mode, clock)
    hold_end = _fan_off_then_thermostat(home, sched, "hot", "off")
    _confirm_override(home, sched)
    assert _cancel(home, "user_cancel_override") is True

    holds = [e for e in _real_episodes(home) if e.kind == "fan_off"]

    assert holds and holds[-1].end_ts is None
    assert abs((holds[-1].grace_end_ts - hold_end).total_seconds()) < 5


@pytest.mark.parametrize("fan_mode", _MODES)
def test_journal_closes_the_fan_run_episode_when_the_fan_card_cancels_it(clock, sched, fan_mode):
    home = _Home(fan_mode, clock)
    _timer_run_then_thermostat_off(home, sched)

    assert _cancel(home, "user_cancel_fan_override") is True

    runs = [e for e in _real_episodes(home) if e.target == "fan" and e.kind == "override"]
    assert runs and runs[-1].end_ts is not None


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize("path", ["fan_card_cancel", "thermostat_cancel_then_end_pause"])
def test_journal_closes_the_fan_off_episode_when_the_pause_is_ended_with_no_fan_off_grace_running(
    clock, sched, fan_mode, path
):
    """Fan off, a thermostat override is confirmed (its grace replaced the fan-off grace), then the occupant ends the
    fan-off pause. No grace_cancelled for a fan_off grace exists, so the engine's fan_off_hold_ended event is what
    closes the journal episode (a listed user_cancel_* end). Otherwise a legitimate CA fan start before the original
    deadline is a false P4 override_respected hit."""
    from custom_components.climate_advisor.invariant_watchdog import _p4_end_is_listed

    home = _Home(fan_mode, clock)
    engine = home.engine
    _fan_off_then_thermostat(home, sched, "hot", "off")
    _confirm_override(home, sched)
    assert engine._last_grace_trigger == "override_confirmed" and engine._fan_off_hold_active()
    if path == "fan_card_cancel":
        assert _cancel(home, "user_cancel_fan_override") is True
    else:
        assert _cancel(home, "user_cancel_override") is True  # thermostat change gone, hold continues, no grace
        assert engine._fan_off_hold_active() and not engine._grace_active
        assert _cancel(home, "user_cancel_override") is True  # the "End Fan-Off Pause" button

    names = [n for _t, n, _d in home.timed_events]
    holds = [e for e in _real_episodes(home) if e.kind == "fan_off"]

    assert holds and holds[-1].end_ts is not None and holds[-1].end_reason.startswith("user_cancel")
    assert _p4_end_is_listed(holds[-1], None)
    assert names.count("fan_off_hold_ended") == 1, "one row, emitted only when no fan-off grace could say it"


@pytest.mark.parametrize("fan_mode", _MODES)
def test_fan_off_hold_ended_is_not_emitted_when_the_fan_off_grace_is_running(clock, sched, fan_mode):
    """With the fan-off grace running its own grace_cancelled closes the episode: no second row, no double close."""
    home = _Home(fan_mode, clock)
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S + 60)
    home.occupant_turns_fan_off()

    assert _cancel(home, "user_cancel_override") is True

    names = [n for _t, n, _d in home.timed_events]
    assert "fan_off_hold_ended" not in names and "grace_cancelled" in names
    holds = [e for e in _real_episodes(home) if e.kind == "fan_off"]
    assert holds and holds[-1].end_reason == "user_cancel_override"


# --- Issue #1205 x #1211: the occupant's fan requests hold inside the planned window period too -------------------
# #1211 removed the "planned window period: just clear the grace" branch of _on_grace_expired(). A grace that expires
# with a window open inside the planned period now takes the re-pause branch (and may hand off to nat-vent). These pin
# that both occupant fan requests survive that new path, with HVAC still paused for the open window (rule 3).


@pytest.fixture
def planned_window_period():
    from custom_components.climate_advisor.automation import AutomationEngine

    with patch.object(AutomationEngine, "_is_within_planned_window_period", lambda self: True):
        yield


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize("tstat", ["off", "heat"])
def test_planned_window_thermostat_grace_expiring_inside_the_fan_off_pause_keeps_the_fan_off(
    clock, sched, planned_window_period, fan_mode, tstat
):
    """Fan off, then a thermostat change confirmed; its grace ends inside the pause with a window open."""
    home = _Home(fan_mode, clock)
    e = home.engine
    hold_end = _fan_off_then_thermostat(home, sched, "hot", tstat)
    _confirm_override(home, sched)
    assert e._last_grace_trigger == "override_confirmed" and e._fan_off_hold_active()
    home.window_open = True
    on_before = len(_fan_on_commands(home))
    ev0 = len(home.events)

    e._grace_end_time = clock.now.isoformat()
    home.expire_grace()

    assert e._fan_off_hold_active() and e._fan_off_hold_until == hold_end
    assert not e._fan_active and len(_fan_on_commands(home)) == on_before
    started = [n for n, _ in home.events[ev0:] if n in ("fan_activated", "nat_vent_reactivated_while_paused")]
    assert not started, home.events[ev0:]
    assert e._paused_by_door, "rule 3: HVAC paused for the open window"
    for _ in range(int((hold_end - clock.now).total_seconds() // 240) - 1):
        clock.advance(240)
        sched.fire_due(home)
        asyncio.run(e.check_natural_vent_conditions())
        asyncio.run(e.nat_vent_temperature_check(72.0, outdoor=65.0))
        home.drain()
        assert not e._fan_active and len(_fan_on_commands(home)) == on_before


@pytest.mark.parametrize("fan_mode", _MODES)
def test_planned_window_fan_run_survives_a_thermostat_grace_expiring_with_a_window_open(
    clock, sched, planned_window_period, fan_mode
):
    """A 4 h remote-timer run, then a thermostat Off confirmed; its grace ends with a window open."""
    home = _Home(fan_mode, clock)
    e = home.engine
    run_end = _timer_run_then_thermostat_off(home, sched)
    assert e._last_grace_trigger == "override_confirmed"

    e._grace_end_time = clock.now.isoformat()
    home.expire_grace()

    assert e._fan_override_active and e._fan_override_until == run_end
    assert e._last_grace_trigger == "fan_manual_override" and e._grace_active
    runs = [x for x in _real_episodes(home) if x.target == "fan" and x.kind == "override"]
    assert runs and runs[-1].end_ts is None
    cleared = [d for n, d in home.events if n == "override_cleared"]
    assert cleared and cleared[-1].get("continues") is True, cleared
