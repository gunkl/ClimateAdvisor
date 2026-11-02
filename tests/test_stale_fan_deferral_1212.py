"""Issue #1212: an abandoned fan start/stop must not leave a stale toggle-floor deferral record behind.

Occupant framing: Climate Advisor sometimes defers a fan start or stop because the fan was toggled less than five
minutes ago (the 5-minute toggle floor). When that start/stop is then abandoned -- the window closes, the occupant
switches the fan off or on themselves, CA's own next request goes the other way -- the whole-house-fan card can keep
saying "(on pending -- 5-min floor, applies at ...)" / "(off pending ...)", and a later unrelated start or stop
logs "5-minute floor expired -- applying deferred activation/exit", possibly hours later. A fresh deferral of the
same moment can even be swallowed as a duplicate (no "Fan toggle deferred" line, no Activity-log row).

The record is ``AutomationEngine._fan_rate_limited_until`` / ``_fan_rate_limited_direction``. #1205 cleared it only
when a manual-origin "pending fact" existed; these tests cover every abandon path that has no pending fact.

Every test drives the REAL engine through the ``_Home`` harness of ``test_stale_fan_marker_1205`` (both supported
fan modes; ``fan_mode: both`` is unsupported, #1175). They were strict xfails until the fix landed: a failure is
always the assertion on the record, the log or the card suffix, never a harness error (route preconditions use
plain asserts / returns so a broken precondition is not mistaken for the symptom).

hvac_fan has no status-card text, so its tests assert the record and the log; the card test is whole_house_fan only.
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from custom_components.climate_advisor.automation import FanCommandResult
from custom_components.climate_advisor.const import FAN_MIN_TOGGLE_INTERVAL_S, FAN_MODE_WHOLE_HOUSE
from tests.test_fan_control import _make_coordinator_for_fan_status
from tests.test_stale_fan_marker_1205 import _AUTOMATION, _MODES, _Clock, _Home

_SENSOR = "binary_sensor.front_window"
_AUTOMATION_LOGGER = "custom_components.climate_advisor.automation"
_cmod = importlib.import_module("custom_components.climate_advisor.coordinator")


@pytest.fixture
def clock():
    """The virtual clock of the ``_Home`` harness (same patches as the #1205 file's fixture)."""
    clk = _Clock()
    with (
        patch(f"{_AUTOMATION}.dt_util.now", side_effect=lambda: clk.now),
        patch(f"{_AUTOMATION}.dt_util.parse_datetime", side_effect=datetime.fromisoformat),
    ):
        yield clk


def _record(engine) -> tuple:
    return (engine._fan_rate_limited_direction, engine._fan_rate_limited_until)


def _assert_cleared(engine, why: str) -> None:
    assert engine._fan_rate_limited_direction is None and engine._fan_rate_limited_until is None, (
        f"stale deferral record survives {why}: direction={engine._fan_rate_limited_direction!r} "
        f"until={engine._fan_rate_limited_until!r}"
    )


# ---------------------------------------------------------------------------
# Routes that produce a deferral record WITHOUT a #1205 pending fact
# ---------------------------------------------------------------------------


def _defer_activation_via_window_open(home: _Home) -> None:
    """CA toggled the fan 30 s ago; the post-debounce window-open handler wants nat-vent: the start is deferred."""
    engine = home.engine
    home.window_open = False
    home.clock.advance(600)
    engine._fan_toggle_command_time = home.clock.now - timedelta(seconds=30)
    home.window_open = True
    asyncio.run(engine.handle_door_window_open(_SENSOR))
    home.drain()
    assert engine._fan_rate_limited_direction == "activate", "precondition: the window-open start was deferred"
    assert not engine._fan_active
    assert engine._fan_manual_origin_pending is None, "precondition: no #1205 pending fact exists on this route"


def _run_fan_then_defer_stop(home: _Home) -> None:
    """CA started the fan, then wants it off 60 s later: the toggle floor defers the stop."""
    engine = home.engine
    asyncio.run(engine._activate_fan(reason="test CA start"))
    home.drain()
    engine._fan_toggle_command_time = home.clock.now
    engine._fan_active = True
    home.clock.advance(60)
    result = asyncio.run(engine._deactivate_fan(reason="test CA stop"))
    home.drain()
    assert result is FanCommandResult.RATE_LIMITED_NEW, f"precondition: stop deferred, got {result}"
    assert engine._fan_rate_limited_direction == "deactivate"
    assert engine._fan_manual_origin_pending is None


def _suffix(home: _Home) -> str:
    """The real card suffix the coordinator appends for this engine's record."""
    with patch.object(_cmod.dt_util, "now", side_effect=lambda: home.clock.now):
        return _cmod.ClimateAdvisorCoordinator._whf_rate_limit_suffix(None, home.engine)


# ---------------------------------------------------------------------------
# F3 / F4 and the other abandon paths: the record must be cleared
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", _MODES)
def test_f3_activation_deferred_then_all_windows_closed_clears_record(clock, fan_mode):
    """F3: a window-open start is deferred by the floor, then every window closes. Nothing is pending any more."""
    home = _Home(fan_mode, clock)
    _defer_activation_via_window_open(home)

    home.close_window()

    _assert_cleared(home.engine, "all windows closing (handle_all_doors_windows_closed)")


@pytest.mark.parametrize("fan_mode", _MODES)
def test_f4_deferred_stop_then_occupant_turns_fan_off_clears_record(clock, fan_mode):
    """F4: CA's stop is deferred by the floor, then the occupant switches the fan off themselves."""
    home = _Home(fan_mode, clock)
    _run_fan_then_defer_stop(home)

    home.occupant_turns_fan_off()

    _assert_cleared(home.engine, "the occupant turning the fan off (on_fan_turned_off)")


@pytest.mark.parametrize("direction", ["activate", "deactivate"])
@pytest.mark.parametrize("fan_mode", _MODES)
def test_occupant_fan_on_clears_a_deferral_of_either_direction(clock, fan_mode, direction):
    """The occupant switches the fan ON; the newest occupant action owns the fan, a deferred CA start/stop is moot."""
    home = _Home(fan_mode, clock)
    if direction == "activate":
        _defer_activation_via_window_open(home)
    else:
        _run_fan_then_defer_stop(home)

    home.engine.handle_fan_manual_override(fan_before="off", fan_after="on", hvac_fan=home.hvac_fan)
    home.drain()

    _assert_cleared(home.engine, f"the occupant's fan-on (handle_fan_manual_override), direction={direction}")


@pytest.mark.parametrize("fan_mode", _MODES)
def test_door_pause_ending_clears_an_activate_record_with_no_pending_fact(clock, fan_mode):
    """The door pause the deferred start belonged to ends (flag True -> False, no hold): the start is abandoned."""
    home = _Home(fan_mode, clock)
    _defer_activation_via_window_open(home)
    engine = home.engine
    engine._paused_by_door = True  # the pause episode the deferral belonged to
    assert getattr(engine, "_door_pause_hold", 0) == 0
    assert engine._fan_rate_limited_direction == "activate"

    engine._paused_by_door = False  # the real setter: the pause ended

    _assert_cleared(engine, "the door pause ending (_paused_by_door True -> False)")


@pytest.mark.parametrize("fan_mode", _MODES)
def test_deferred_stop_then_ca_start_already_in_state_clears_deactivate_record(clock, fan_mode):
    """Opposite direction: CA deferred a stop, then CA wants the fan ON; the fan still runs -> ALREADY_IN_STATE.

    The stop is no longer wanted, so "(off pending ...)" must go.
    """
    home = _Home(fan_mode, clock)
    _run_fan_then_defer_stop(home)
    home.clock.advance(30)

    result = asyncio.run(home.engine._activate_fan(reason="test CA wants the fan on again"))
    home.drain()

    assert result is FanCommandResult.ALREADY_IN_STATE, f"precondition: the start is a no-op, got {result}"
    _assert_cleared(home.engine, "CA's opposite-direction start (ALREADY_IN_STATE)")


@pytest.mark.parametrize("fan_mode", _MODES)
def test_deferred_start_then_ca_stop_already_in_state_clears_activate_record(clock, fan_mode):
    """Mirror: CA deferred a start, then CA wants the fan OFF; the fan is off -> ALREADY_IN_STATE."""
    home = _Home(fan_mode, clock)
    _defer_activation_via_window_open(home)
    home.clock.advance(30)

    result = asyncio.run(home.engine._deactivate_fan(reason="test CA wants the fan off"))
    home.drain()

    assert result is FanCommandResult.ALREADY_IN_STATE, f"precondition: the stop is a no-op, got {result}"
    _assert_cleared(home.engine, "CA's opposite-direction stop (ALREADY_IN_STATE)")


def test_occupant_press_guard_early_return_still_clears_opposite_record(clock):
    """whole_house_fan only: ``_occupant_fan_press_blocks_start`` returns before ALREADY_IN_STATE; the opposite
    record must still go.

    Not applicable to hvac_fan: the guard is ``fan_mode in (WHOLE_HOUSE, BOTH) and ...``, so it can never trigger
    for the thermostat blower.
    """
    home = _Home(FAN_MODE_WHOLE_HOUSE, clock)
    _run_fan_then_defer_stop(home)
    engine = home.engine
    engine._occupant_fan_action_at = home.clock.now  # a person pressed the remote just now (#1215 settling window)
    assert engine._occupant_fan_press_blocks_start(FAN_MODE_WHOLE_HOUSE)
    assert not engine._fan_override_active
    assert not engine._fan_off_hold_active()

    result = asyncio.run(engine._activate_fan(reason="test CA start during the press settle window"))
    home.drain()

    assert result is FanCommandResult.SUPPRESSED, f"precondition: the press guard refused the start, got {result}"
    _assert_cleared(engine, "CA's opposite-direction start refused by the occupant-press guard")


# ---------------------------------------------------------------------------
# What the occupant / operator sees afterwards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", _MODES)
def test_hours_after_an_abandoned_activation_an_unrelated_start_is_not_a_deferred_one(clock, fan_mode, caplog):
    """F3 then an hour later an unrelated start: the log must not claim the 5-minute floor expired for it."""
    caplog.set_level("INFO", logger=_AUTOMATION_LOGGER)
    home = _Home(fan_mode, clock)
    _defer_activation_via_window_open(home)
    home.close_window()
    home.clock.advance(3600)
    home.window_open = True  # the window is open again, so a whole-house-fan start is allowed
    caplog.clear()

    result = asyncio.run(home.engine._activate_fan(reason="test unrelated start an hour later"))
    home.drain()

    assert result is FanCommandResult.EXECUTED, f"precondition: the late start ran, got {result}"
    assert "applying deferred activation" not in caplog.text, "an hour-old abandoned deferral was 'applied'"


@pytest.mark.parametrize("fan_mode", _MODES)
def test_hours_after_an_abandoned_stop_an_unrelated_stop_is_not_a_deferred_one(clock, fan_mode, caplog):
    """Opposite-direction abandon (CA turned its mind), then an hour later an unrelated stop: no 'deferred exit'."""
    caplog.set_level("INFO", logger=_AUTOMATION_LOGGER)
    home = _Home(fan_mode, clock)
    _run_fan_then_defer_stop(home)
    home.clock.advance(30)
    asyncio.run(home.engine._activate_fan(reason="test CA wants the fan on again"))  # abandons the stop
    home.drain()
    home.clock.advance(3600)
    caplog.clear()

    result = asyncio.run(home.engine._deactivate_fan(reason="test unrelated stop an hour later"))
    home.drain()

    assert result is FanCommandResult.EXECUTED, f"precondition: the late stop ran, got {result}"
    assert "applying deferred exit" not in caplog.text, "an hour-old abandoned deferral was 'applied'"


@pytest.mark.parametrize("fan_mode", _MODES)
def test_stale_record_does_not_swallow_a_fresh_same_direction_deferral(clock, fan_mode, caplog):
    """RATE_LIMITED_DUP: after the occupant's off abandoned a deferred stop, the engine believes the fan is on
    again (a reconcile adopt does not stamp the toggle time) and CA's new stop is deferred for the SAME moment. It is
    a fresh deferral and must be reported as such (INFO "Fan toggle deferred", RATE_LIMITED_NEW), not swallowed as a
    duplicate of the abandoned one."""
    caplog.set_level("INFO", logger=_AUTOMATION_LOGGER)
    home = _Home(fan_mode, clock)
    engine = home.engine
    _run_fan_then_defer_stop(home)
    home.occupant_turns_fan_off()  # abandons the deferred stop
    engine._fan_active = True  # the engine adopts a running fan again without any new toggle stamp
    home.clock.advance(30)
    assert home.clock.now - engine._fan_toggle_command_time < timedelta(seconds=FAN_MIN_TOGGLE_INTERVAL_S)
    caplog.clear()

    result = asyncio.run(engine._deactivate_fan(reason="test second, unrelated stop"))
    home.drain()

    assert result is FanCommandResult.RATE_LIMITED_NEW, f"fresh deferral reported as {result}"
    assert "Fan toggle deferred" in caplog.text


@pytest.mark.parametrize(
    "abandon",
    ["windows_closed", "occupant_fan_on", "ca_stop_already_in_state"],
)
def test_whf_card_has_no_pending_suffix_after_an_abandoned_activation(clock, abandon):
    """whole_house_fan only (hvac_fan has no card text): the WHF status card must stop saying '(on pending ...)'."""
    home = _Home(FAN_MODE_WHOLE_HOUSE, clock)
    _defer_activation_via_window_open(home)
    assert "on pending" in _suffix(home), "precondition: the card shows the pending start while it is deferred"
    if abandon == "windows_closed":
        home.close_window()
    elif abandon == "occupant_fan_on":
        home.engine.handle_fan_manual_override(fan_before="off", fan_after="on", hvac_fan=False)
        home.drain()
    else:
        asyncio.run(home.engine._deactivate_fan(reason="test CA wants the fan off"))
        home.drain()

    assert _suffix(home) == "", f"stale card suffix after the start was abandoned ({abandon}): {_suffix(home)!r}"


def test_whf_compute_status_has_no_pending_suffix_after_an_abandoned_stop(clock):
    """Same, through the real ``_compute_whf_status`` of a coordinator wrapped around the real engine."""
    home = _Home(FAN_MODE_WHOLE_HOUSE, clock)
    _run_fan_then_defer_stop(home)
    coord = _make_coordinator_for_fan_status(fan_mode=FAN_MODE_WHOLE_HOUSE, physical_state=None)
    coord.automation_engine = home.engine
    with patch.object(_cmod.dt_util, "now", side_effect=lambda: home.clock.now):
        assert "off pending" in coord._compute_whf_status(), "precondition: the card shows the pending stop"
        home.occupant_turns_fan_off()  # the stop is abandoned
        status = coord._compute_whf_status()

    assert "pending" not in status, f"stale pending suffix on the WHF status card: {status!r}"


# ---------------------------------------------------------------------------
# NOT an abandon: the reconcile "no-fan" bookkeeping stop keeps a deferred start that is still retried
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", _MODES)
def test_reconcile_no_fan_branch_keeps_a_deferred_reactivation_that_is_still_retried(clock, fan_mode, caplog):
    """The reconcile "no-fan" branch (fan confirmed off -> release any stranded HVAC suppression) is bookkeeping,
    not CA deciding the fan should be off, so it must NOT abandon a deferred start (#1212 step 2c opt-out).

    Occupant story: a window is open, CA's grace ends inside the toggle floor, so CA's restart of the fan is deferred
    (a #1205 pending fact is raised). Before the floor ends the thermostat's own blower cycles (hvac_action -> fan;
    a whole-house-fan install reads its real fan, which is off), so the coordinator runs the reconcile and it takes
    the "no-fan" branch. The paused-reactivation retry (check_natural_vent_conditions, which never reads the record)
    still restarts the fan once the floor ends -- and that run must carry the "restarted after your grace ended"
    marker, so the occupant's later off gets the right wording. Clearing here dropped the pending fact and lost it.
    """
    caplog.set_level("INFO", logger=_AUTOMATION_LOGGER)
    home = _Home(fan_mode, clock)
    engine = home.engine
    engine._start_grace_period("automation", "nat_vent_exit_resume")
    remaining = (datetime.fromisoformat(engine._grace_end_time) - home.clock.now).total_seconds()
    home.clock.advance(remaining - 10)
    engine._fan_toggle_command_time = home.clock.now
    home.expire_grace()
    assert engine._fan_rate_limited_direction == "activate", "precondition: the reactivation was deferred"
    pending = engine._fan_manual_origin_pending
    assert pending is not None, "precondition: the #1205 pending fact was raised"
    assert engine._paused_by_door, "precondition: re-paused for the open window"
    assert not engine._fan_active and not engine._natural_vent_active

    home.clock.advance(60)  # past the reconcile's own 30 s fresh-command guard, still inside the floor
    engine._is_recent_fan_command_callback = lambda threshold_seconds=30.0: False
    caplog.clear()
    asyncio.run(
        engine.reconcile_fan_on_startup(
            indoor=72.0,
            outdoor=65.0,
            thermostat_fan_running=False,
            any_sensor_open=True,
            trigger="thermostat_state_change",
        )
    )
    home.drain()
    assert "decision=no-fan" in caplog.text, "precondition: the reconcile took the no-fan branch"

    assert engine._fan_rate_limited_direction == "activate", "the reconcile abandoned a start that is still retried"
    assert engine._fan_manual_origin_pending == pending, "the reconcile dropped the #1205 pending fact"

    # The floor ends with the window still open: the paused-reactivation retry applies the deferred start.
    home.clock.advance(FAN_MIN_TOGGLE_INTERVAL_S)
    asyncio.run(engine.check_natural_vent_conditions())
    home.drain()
    assert engine._fan_active, "precondition: the deferred reactivation applied"
    assert "applying deferred activation" in caplog.text
    assert engine._fan_manual_origin_unclosed is True, "the restarted run lost its #1205 'restart' marker"
    assert engine._fan_manual_origin_kind == "restart"
    _assert_cleared(engine, "the deferred start applying")
