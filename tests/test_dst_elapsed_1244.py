"""Issue #1244: elapsed time since a stored START instant must be real time across a daylight-saving change.

Occupant stories (America/Los_Angeles; fall-back 2026-11-01 02:00 PDT -> 01:00 PST, spring-forward 2026-03-08
02:00 PST -> 03:00 PDT):

* Fall-back. Climate Advisor turns the thermostat to cool at 01:45 PDT. 25 real minutes later, at 01:10 PST in the
  repeated hour, the occupant changes it. Climate Advisor used to compute "01:10 - 01:45 = -35 min", read that as
  "less than 120 s ago" and took the occupant's change for its own command's echo: no override, no grace, and the
  next cycle could write over the occupant's choice.
* The same negative elapsed time kept the 5-minute fan toggle floor and the nat-vent re-entry lockout running for
  up to an hour after they had really ended, and suppressed a welcome-home notice that was due.
* Spring-forward. Elapsed time read an hour too long: the 5-minute fan floor and the lockout ended early, and the
  welcome-home spacing let a second notice through 30 real minutes after the first.
* The blind-fan auto-stop (30 minutes of an unreadable indoor sensor) stopped an hour late on fall-back night,
  and the Fan card's "stops at" time showed the wall-clock sum, not the real stop time.

A start stored as ``dt_util.now()`` carries the local ``ZoneInfo``; Python subtracts two such datetimes in
wall-clock time and ignores the fold. Every elapsed read now goes through ``time_utils.seconds_since``.
Fixtures come from ``tests/test_dst_grace_end_1199.py`` (the zone is set per test; conftest restores UTC).
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor import i18n
from custom_components.climate_advisor.automation import FanCommandResult
from custom_components.climate_advisor.confirmed_transition import is_confirmed
from custom_components.climate_advisor.const import (
    CONF_FAN_MODE,
    FAN_MIN_TOGGLE_INTERVAL_S,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
    INDOOR_BLIND_FAN_STOP_S,
    NAT_VENT_REACTIVATION_LOCKOUT_S,
)
from custom_components.climate_advisor.fan_toggle_rate_limit import (
    FanToggleRateLimitInputs,
    FanToggleRateLimitOutcome,
    decide_fan_toggle_rate_limit,
)
from custom_components.climate_advisor.nat_vent_reactivation_lockout import is_reactivation_locked_out
from custom_components.climate_advisor.occupancy_fsm import HomeNotifyOutcome
from custom_components.climate_advisor.temperature import reminder_due
from custom_components.climate_advisor.time_utils import seconds_since
from tests.test_dst_grace_end_1199 import _LA, _los_angeles, _make_clock  # noqa: F401  (autouse fixture)
from tests.test_restore_resolved_band import _make_engine
from tests.test_stale_fan_marker_1205 import _classification, _Home, _unrelated_ordinary_ca_start

_MODES = [FAN_MODE_WHOLE_HOUSE, FAN_MODE_HVAC]


def _la(utc_h: int, utc_m: int, *, day: tuple[int, int, int] = (2026, 11, 1), s: int = 0) -> datetime:
    """The local (Los Angeles ``ZoneInfo``) reading of a UTC instant, exactly what ``dt_util.now()`` stores."""
    return datetime(*day, utc_h, utc_m, s, tzinfo=UTC).astimezone(_LA)


# Fall-back: 01:58 PDT (08:58Z) -> 01:04 PST (09:04Z) is 6 real minutes; wall clock says -54 min.
_FB_START, _FB_NOW = _la(8, 58), _la(9, 4)
# Spring-forward: 01:58 PST (09:58Z) -> 03:01 PDT (10:01Z) is 3 real minutes; wall clock says 63 min.
_SF_START, _SF_NOW = _la(9, 58, day=(2026, 3, 8)), _la(10, 1, day=(2026, 3, 8))
_PAIRS = [
    pytest.param(_FB_START, _FB_NOW, 360.0, id="fall_back"),
    pytest.param(_SF_START, _SF_NOW, 180.0, id="spring_forward"),
]


def test_fixture_pairs_really_are_wall_clock_wrong():
    """Precondition: the raw subtraction is wrong for these pairs (else every test below is vacuous)."""
    assert _FB_START.tzinfo is _FB_NOW.tzinfo
    assert (_FB_NOW - _FB_START).total_seconds() == -54 * 60
    assert (_SF_NOW - _SF_START).total_seconds() == 63 * 60


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("start", "now", "real_s"), _PAIRS)
def test_seconds_since_is_real_time(monkeypatch, start, now, real_s):
    assert seconds_since(start, now=now) == real_s
    _make_clock(monkeypatch, now)
    assert seconds_since(start) == real_s, "defaults to dt_util.now()"
    assert seconds_since(start.astimezone(UTC), now=now) == real_s, "a UTC start mixes with a local now"


def test_seconds_since_keeps_the_raw_subtraction_contract_for_naive_clocks():
    """Two naive values subtract as before; a naive/aware mix raises TypeError as before (callers' fallbacks)."""
    assert seconds_since(datetime(2026, 11, 1, 1, 58), now=datetime(2026, 11, 1, 2, 4)) == 360.0
    with pytest.raises(TypeError):
        seconds_since(datetime(2026, 11, 1, 1, 58), now=_FB_NOW)
    with pytest.raises(TypeError):
        seconds_since(_FB_START, now=datetime(2026, 11, 1, 2, 4))


# ---------------------------------------------------------------------------
# Pure choke points: nat-vent re-entry lockout, fan toggle floor, confirmed transition, reminders
# ---------------------------------------------------------------------------


def test_nat_vent_lockout_ends_after_real_five_minutes_on_fall_back_night():
    """Exit 01:58 PDT, 6 real minutes later (01:04 PST): the 5-minute lockout is over, nat-vent may resume."""
    assert not is_reactivation_locked_out(
        outdoor_exit_time=_FB_START, now=_FB_NOW, lockout_seconds=NAT_VENT_REACTIVATION_LOCKOUT_S
    )


def test_nat_vent_lockout_still_holds_three_real_minutes_after_spring_forward():
    assert is_reactivation_locked_out(
        outdoor_exit_time=_SF_START, now=_SF_NOW, lockout_seconds=NAT_VENT_REACTIVATION_LOCKOUT_S
    )


@pytest.mark.parametrize(
    ("start", "now", "expected"),
    [
        pytest.param(_FB_START, _FB_NOW, FanToggleRateLimitOutcome.ALLOW, id="fall_back"),
        pytest.param(_SF_START, _SF_NOW, FanToggleRateLimitOutcome.DEFER_NEW, id="spring_forward"),
    ],
)
def test_fan_toggle_floor_is_five_real_minutes(start, now, expected):
    outcome, applies_at = decide_fan_toggle_rate_limit(
        FanToggleRateLimitInputs(
            last_toggle_command_time=start,
            action="deactivate",
            rate_limited_until=None,
            rate_limited_direction=None,
            min_interval_s=FAN_MIN_TOGGLE_INTERVAL_S,
            now=now,
        )
    )
    assert outcome is expected
    if expected is FanToggleRateLimitOutcome.DEFER_NEW:
        assert applies_at == start.astimezone(UTC) + timedelta(seconds=FAN_MIN_TOGGLE_INTERVAL_S)


@pytest.mark.parametrize(
    ("since", "now", "confirmed"),
    [
        # 01:59:00 PDT -> 01:00:31 PST = 91 real s (wall clock: -58:29)
        pytest.param(_la(8, 59), _la(9, 0, s=31), True, id="fall_back_91s"),
        # 01:59:00 PST -> 03:00:00 PDT = 60 real s (wall clock: 61 min)
        pytest.param(_la(9, 59, day=(2026, 3, 8)), _la(10, 0, day=(2026, 3, 8)), False, id="spring_forward_60s"),
    ],
)
def test_confirmed_transition_sustain_is_real_time(since, now, confirmed):
    assert is_confirmed(candidate="x", candidate_since=since, now=now, sustain_seconds=90) is confirmed


@pytest.mark.parametrize(("start", "now", "real_s"), _PAIRS)
def test_reminder_due_counts_real_time(start, now, real_s):
    assert reminder_due(start, now, real_s) is True
    assert reminder_due(start, now, real_s + 1) is False


# ---------------------------------------------------------------------------
# Echo / race guards: the occupant's change in the repeated hour is the occupant, not CA's echo
# ---------------------------------------------------------------------------

_CA_CMD = _la(8, 45)  # 01:45 PDT
_OCCUPANT = _la(9, 10)  # 01:10 PST, 25 real minutes later


def _coordinator_with_commands(at: datetime):
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    coord = object.__new__(mod.ClimateAdvisorCoordinator)
    coord.automation_engine = SimpleNamespace(_hvac_command_time=at, _temp_command_time=at, _fan_command_time=at)
    return coord


@pytest.mark.parametrize("guard", ["_is_recent_hvac_command", "_is_recent_temp_command", "_is_recent_fan_command"])
def test_race_guards_see_the_occupant_25_real_minutes_after_ca(monkeypatch, guard):
    _make_clock(monkeypatch, _OCCUPANT)
    coord = _coordinator_with_commands(_CA_CMD)
    assert getattr(coord, guard)(threshold_seconds=120.0) is False


@pytest.mark.parametrize("guard", ["_is_recent_hvac_command", "_is_recent_temp_command", "_is_recent_fan_command"])
def test_race_guards_still_hold_seconds_after_spring_forward(monkeypatch, guard):
    """CA command 01:59:50 PST, now 03:00:00 PDT: 10 real seconds, still CA's own echo window."""
    _make_clock(monkeypatch, _la(10, 0, day=(2026, 3, 8)))
    coord = _coordinator_with_commands(_la(9, 59, day=(2026, 3, 8), s=50))
    assert getattr(coord, guard)(threshold_seconds=30.0) is True


def test_occupant_mode_change_in_repeated_hour_is_an_override(monkeypatch):
    """The 120 s expected-confirmation must not swallow the occupant's change 25 real minutes after CA's command."""
    from tests.test_override_automation_boundary import _make_event, _make_thermostat_coord_stub

    _make_clock(monkeypatch, _OCCUPANT)
    coord = _make_thermostat_coord_stub(
        hvac_command_pending=False, fan_command_pending=False, temp_command_pending=False, paused_by_door=True
    )
    coord.automation_engine._last_commanded_hvac_mode = "cool"
    coord.automation_engine._last_commanded_hvac_time = _CA_CMD
    asyncio.run(coord._async_thermostat_changed(_make_event("off", "cool")))
    coord.automation_engine.handle_manual_override_during_pause.assert_called_once()


# ---------------------------------------------------------------------------
# The real engine: fan toggle floor (both fan modes), welcome-home spacing, occupant press, blind fan
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize(
    ("start_utc", "after_s", "deferred"),
    [
        pytest.param(datetime(2026, 11, 1, 8, 58, tzinfo=UTC), 360.0, False, id="fall_back"),
        pytest.param(datetime(2026, 3, 8, 9, 58, tzinfo=UTC), 180.0, True, id="spring_forward"),
    ],
)
def test_engine_fan_floor_is_five_real_minutes(monkeypatch, fan_mode, start_utc, after_s, deferred):
    """CA starts the fan at 01:58 (PDT / PST); asking to stop it later obeys the real 5-minute floor."""
    clk = _make_clock(monkeypatch, start_utc - timedelta(seconds=FAN_MIN_TOGGLE_INTERVAL_S + 60 + 600))
    home = _Home(fan_mode, clk)
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    assert clk.now == start_utc, "precondition: the fan start happens at the scenario instant"
    engine = home.engine
    if fan_mode == FAN_MODE_HVAC:
        assert engine._fan_toggle_command_time.astimezone(UTC) == start_utc, "the blower start stamps the floor"
    else:
        # This harness's whole-house fan start takes the forced (off, then on in 5 s) path, whose second step is a
        # timer the harness does not fire; stamp the floor as that step's real command does (dt_util.now(), local).
        engine._fan_toggle_command_time = sys.modules["homeassistant.util.dt"].now()
    clk.advance(after_s)
    result = asyncio.run(home.engine._deactivate_fan(reason="test"))
    home.drain()
    if deferred:
        assert result is FanCommandResult.RATE_LIMITED_NEW, "3 real minutes: the floor must still hold"
    else:
        assert result not in (FanCommandResult.RATE_LIMITED_NEW, FanCommandResult.RATE_LIMITED_DUP), result
        assert not home.engine._fan_active


@pytest.mark.parametrize(
    ("notified", "arrive", "expected"),
    [
        # 00:50 PDT -> 01:20 PST is 90 real minutes (wall clock 30): past the 60-minute spacing
        pytest.param(_la(7, 50), _la(9, 20), HomeNotifyOutcome.SEND, id="fall_back"),
        # 01:30 PST -> 03:00 PDT is 30 real minutes (wall clock 90): still inside the spacing
        pytest.param(
            _la(9, 30, day=(2026, 3, 8)),
            _la(10, 0, day=(2026, 3, 8)),
            HomeNotifyOutcome.SUPPRESSED_DEBOUNCE,
            id="spring_forward",
        ),
    ],
)
def test_welcome_home_spacing_is_real_time(monkeypatch, notified, arrive, expected):
    _make_clock(monkeypatch, arrive)
    engine = _make_engine(indoor=72.0, mode="cool")
    engine._current_classification = _classification("cold")
    engine._get_indoor_temp_f = lambda: None  # no proximity check: the spacing alone decides
    engine._last_welcome_home_notified = notified
    assert engine._resolve_occupancy_home_fsm_state().notify is expected


def test_occupant_fan_press_pending_window_is_real_time(monkeypatch):
    """A press at 01:59:55 PDT is still pending 10 real seconds later (01:00:05 PST, wall clock -59:50)."""
    _make_clock(monkeypatch, datetime(2026, 11, 1, 9, 0, 5, tzinfo=UTC))
    engine = _make_engine(indoor=72.0, mode="cool")
    engine._occupant_fan_action_at = _la(8, 59, s=55)
    assert engine._occupant_fan_action_pending() is True


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize(
    ("blind_from_utc", "local_stop"),
    [
        # Blind from 01:40 PDT: 30 real minutes end at 01:10 PST (the wall-clock sum said 02:10)
        pytest.param(datetime(2026, 11, 1, 8, 40, tzinfo=UTC), "1:10 AM", id="fall_back"),
        # Blind from 01:40 PST: 30 real minutes end at 03:10 PDT
        pytest.param(datetime(2026, 3, 8, 9, 40, tzinfo=UTC), "3:10 AM", id="spring_forward"),
    ],
)
def test_blind_fan_stops_after_thirty_real_minutes(monkeypatch, caplog, fan_mode, blind_from_utc, local_stop):
    from tests.test_indoor_blind_fan_stop import _Rig

    clk = _make_clock(monkeypatch, blind_from_utc)
    rig = _Rig()
    rig.engine.config[CONF_FAN_MODE] = fan_mode
    rig.start_nat_vent()
    rig.blind()
    true_stop = blind_from_utc + timedelta(seconds=INDOOR_BLIND_FAN_STOP_S)
    assert f"stop_at={true_stop.astimezone(_LA):%H:%M}" in caplog.text, "the WARNING names the local stop time"

    info = rig.engine.indoor_blind_info
    assert info["stop_at"] == true_stop, info
    started = rig.types("fan_blind_started")[0]
    assert datetime.fromisoformat(started["stop_at"]) == true_stop

    # The Fan card's suffix shows the true local stop time.
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    coord = object.__new__(mod.ClimateAdvisorCoordinator)
    coord.automation_engine = rig.engine
    suffix = types.MethodType(mod.ClimateAdvisorCoordinator._append_indoor_blind_suffix, coord)("active", card="whf")
    assert local_stop in i18n.render_en(suffix) if isinstance(suffix, i18n.Msg) else local_stop in str(suffix)

    # Backstop ticks every 5 minutes: no stop before 30 real minutes, the stop on the tick at 30.
    for _ in range(5):
        clk.advance(300)
        rig.blind()
    rig.engine._exit_nat_vent.assert_not_awaited()
    clk.advance(300)
    rig.blind()
    rig.engine._exit_nat_vent.assert_awaited_once()
    assert clk.now == true_stop
    assert rig.engine._exit_nat_vent.await_args.kwargs["event_payload"]["minutes_blind"] == 30


def test_blind_fan_recovery_reports_real_minutes(monkeypatch):
    from tests.test_indoor_blind_fan_stop import _Rig

    clk = _make_clock(monkeypatch, datetime(2026, 11, 1, 8, 50, tzinfo=UTC))  # 01:50 PDT
    rig = _Rig()
    rig.start_nat_vent()
    rig.blind()
    for _ in range(4):  # 20 real minutes, ending 01:10 PST
        clk.advance(300)
        rig.blind()
    rig.engine.note_indoor_available(72.0)
    assert rig.types("fan_blind_recovered")[0]["minutes_blind"] == 20


# ---------------------------------------------------------------------------
# Daily records: comfort-violation minutes never go negative across fall-back
# ---------------------------------------------------------------------------


def test_comfort_violation_minutes_count_real_time(monkeypatch):
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    _make_clock(monkeypatch, _FB_NOW)
    coord = object.__new__(mod.ClimateAdvisorCoordinator)
    coord._indoor_temp_history = []
    coord._today_record = SimpleNamespace(comfort_violations_minutes=0.0)
    coord._last_violation_check = _FB_START
    coord._comfort_score_bank_bands = lambda now: None
    coord._comfort_score_band_now = lambda: (68.0, 74.0)
    coord._is_tolerated_comfort_deviation = lambda *_a: False
    coord._track_indoor_comfort(90.0)  # well outside the band
    assert coord._today_record.comfort_violations_minutes == pytest.approx(6.0)


def test_comfort_stall_counts_a_heating_run_in_the_repeated_hour():
    """The house left the band at 01:50 PDT; heating ran at 01:10 PST (20 real minutes later). At 01:20 PST the
    heating has been running for the house, so no "heating is not keeping up" stall alert yet (before: the run read
    as earlier than the band exit and the alert was confirmed after 30 wall-clock-wrong minutes)."""
    from custom_components.climate_advisor.comfort_stall_decision import decide_comfort_stall

    since, ran, now = _la(8, 50), _la(9, 10), _la(9, 20)
    decision = decide_comfort_stall(
        indoor=60.0,
        comfort_heat=68.0,
        comfort_cool=74.0,
        now=now,
        heat_margin_f=1.0,
        cool_margin_f=1.0,
        sustain_seconds=1800,
        previous_direction="heat",
        previous_since=since,
        last_active={"heat": ran},
        thermostat_available=True,
        has_hvac_action=True,
        hvac_modes=["heat", "cool", "off"],
        manual_control=False,
        sensor_open=False,
        startup_coalescing=False,
        nat_vent_tolerated=False,
    )
    assert decision.effective_since is ran
    assert decision.confirmed is False


# ---------------------------------------------------------------------------
# Comparisons between two stored local instants (intra-zone ``>`` also ignores the fold)
# ---------------------------------------------------------------------------


def test_comfort_family_dwell_runs_from_the_really_latest_mode_change():
    """Selector switch at 01:50 PDT, then a thermostat mode change at 01:05 PST (15 real minutes later). At 01:12 PST
    the 10-minute anti-chatter dwell still runs from 01:05 PST: no family switch yet (before: 01:50 read as the later
    mark, and the dwell was over)."""
    from custom_components.climate_advisor.comfort_family_fsm import (
        ComfortFamilyDwellState,
        ComfortFamilyEvent,
        ComfortFamilyEventKind,
        ComfortFamilyFsmInputs,
        ComfortFamilyState,
        transition,
    )

    def _event(now, last_mode_change_at):
        return ComfortFamilyEvent(
            kind=ComfortFamilyEventKind.TICK,
            inputs=ComfortFamilyFsmInputs(
                default_family="cooling",
                indoor=73.0,
                floor=68.0,
                ceiling=74.0,
                margin_f=2.0,
                natural_vent_active=False,
                whf_owns_hvac=False,
                now=now,
                min_dwell_seconds=600.0,
                last_mode_change_at=last_mode_change_at,
            ),
        )

    heating = ComfortFamilyState.HEATING
    dwell = ComfortFamilyDwellState(dwell_since=_la(8, 50))  # 01:50 PDT
    assert transition(heating, _event(_la(9, 12), _la(9, 5)), dwell).locked_out is True
    # 01:45 PDT -> 01:00 PST is 15 real minutes: the dwell is over and the switch goes ahead.
    t = transition(heating, _event(_la(9, 0), None), ComfortFamilyDwellState(dwell_since=_la(8, 45)))
    assert t.locked_out is False and t.to_state is ComfortFamilyState.COOLING


def test_whf_forced_start_second_step_respects_an_occupant_press_in_the_gap(monkeypatch):
    """CA's forced start (off, then on 5 s later) begins at 01:59:58 PDT; the occupant presses the fan off at
    01:00:01 PST, 3 real seconds later, inside the gap. The second step must not switch the fan back on."""
    from tests.test_fan_control import TestCommandWhfControlEntity

    _make_clock(monkeypatch, datetime(2026, 11, 1, 9, 0, 3, tzinfo=UTC))
    engine = TestCommandWhfControlEntity._engine(None, control_state="on", ground_truth=False)
    engine._fan_active = True
    engine._whf_forced_start_at = _la(8, 59, s=58)
    engine._occupant_fan_action_at = _la(9, 0, s=1)
    asyncio.run(engine._finish_forced_whf_start("fan.whf", "fan", "test"))
    turned_on = [c for c in engine.hass.services.async_call.call_args_list if c.args[:2] == ("fan", "turn_on")]
    assert not turned_on, "CA switched the fan back on over the occupant's press"


def test_remote_offset_startup_feedback_counts_across_fall_back():
    """A remote-sensor raise at 01:58 PDT and the heat starting 01:00 PST (2 real minutes later) is the raise's
    feedback (before: -58 minutes read as 'before the raise' and the feedback was dropped)."""
    engine = _make_engine(indoor=72.0, mode="heat")
    engine._remote_raise_pending_feedback = True
    engine._remote_raise_at = _la(8, 58)
    engine._remote_target_mode = "heat"
    engine._remote_startup_latency_s = None
    engine.note_remote_offset_actuation("heat", _la(9, 0))
    assert engine._remote_raise_pending_feedback is False
    assert engine._remote_startup_latency_s == 120.0
