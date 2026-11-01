"""Issues #1133, #1148, #1147, #1131, #1146, #1152: the occupant's manual change is never undone by Climate Advisor.

Owner rules (authoritative, 2026-10-31):

1. HVAC stays off while any window is open, UNLESS a manual override turns it on (the manual change wins).
2. The whole-house fan never runs on a sealed house.
3. WHF and HVAC are mutually exclusive.

Occupant framing, one line per issue:

* #1133  Manual Change Pause = 0. The occupant turns the heat on; hours later a window opens and CA switches
  the heat OFF over their change, while bedtime and wake-up still skip "because of the manual override".
* #1148  The occupant sets heat (manual grace running) and opens a window while the air outside would also
  make a nat-vent session viable: CA paused their heat 4.5 minutes before the grace ended.
* #1147  The occupant sets the thermostat Off during natural ventilation and closes the window: CA turns the
  AC back on about 14 minutes into the Off grace.
* #1131  The occupant raises the SETPOINT while a door pause has the thermostat Off. When the door closes CA
  restores its own mode AND its own setpoint, and the occupant's number is gone.
* #1146  The occupant changes only the temperature (mode already matches the day): the change was never
  protected -- a window opening turned HVAC off over it and the status card showed no grace.
* #1152  The dead coordinator copy of the override/grace FSM that carried the #1146 defect is gone.

Real ``AutomationEngine`` + the fake thermostat from ``tests.test_restore_resolved_band``. Overrides are made
through the real entry points (``handle_manual_override`` / the captured confirmation timer), never by hand-set
flags where a real path exists. Nothing here re-implements production logic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.const import (
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    FAN_MODE_WHOLE_HOUSE,
)
from custom_components.climate_advisor.override_grace_lifecycle import GraceState, OverrideConfirmState
from tests.test_restore_resolved_band import _classification, _make_engine, _run

_AUTOMATION = "custom_components.climate_advisor.automation"
_DAY = datetime(2026, 10, 28, 14, 0, 0, tzinfo=UTC)
_WINDOW = "binary_sensor.window"

_PROTECTING = (OverrideConfirmState.IDLE, GraceState.ACTIVE_PROTECTING_OVERRIDE)
_NOTHING = (OverrideConfirmState.IDLE, GraceState.NONE)


class _Timers:
    """Captures ``async_call_later`` registrations so a test fires the confirmation / grace timers itself."""

    def __init__(self) -> None:
        self.pending: list[tuple[float, object]] = []

    def __call__(self, _hass, delay, fn):
        self.pending.append((delay, fn))
        return MagicMock()

    def fire(self, delay: float) -> None:
        match = [fn for d, fn in self.pending if d == delay]
        assert match, f"no timer registered for {delay}s: {[d for d, _ in self.pending]}"
        match[-1](None)


@pytest.fixture
def timers():
    t = _Timers()
    with (
        patch(f"{_AUTOMATION}.async_call_later", side_effect=t),
        patch(f"{_AUTOMATION}.callback", side_effect=lambda fn: fn),
        patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY),
        patch(f"{_AUTOMATION}.dt_util.parse_datetime", side_effect=datetime.fromisoformat),
    ):
        yield t


def _engine(
    mode: str = "heat",
    *,
    indoor: float = 70.0,
    setpoint: float = 68.0,
    day: str = "mild",
    outdoor: float | None = 95.0,
    window_open: bool = False,
    **cfg,
) -> AutomationEngine:
    """An engine on a thermostat reading ``mode``; ``outdoor`` defaults to hot so nat-vent is NOT viable."""
    engine = _make_engine(indoor=indoor, mode=mode, **cfg)
    engine._test_state.attributes["temperature"] = setpoint
    engine._current_classification = _classification(day)
    engine._last_outdoor_temp = outdoor
    engine._hourly_forecast_temps = []
    engine._window_open = window_open
    engine._sensor_check_callback = lambda: engine._window_open
    engine._sensor_debounce_pending_callback = lambda: False
    return engine


def _occupant_overrides(engine: AutomationEngine, *, source: str = "normal", new_mode: str | None = None, **kw) -> None:
    """The occupant's change, detected through the real entry point."""
    engine.handle_manual_override(
        source=source,
        old_mode="off" if source == "setpoint" else "cool",
        new_mode=new_mode or engine._test_state.state,
        classification_mode=engine._current_classification.hvac_mode,
        **kw,
    )


def _climate_writes(engine: AutomationEngine) -> list[tuple[str, dict]]:
    return [
        (c.args[1], dict(c.args[2])) for c in engine.hass.services.async_call.call_args_list if c.args[0] == "climate"
    ]


def _off_writes(engine: AutomationEngine) -> list[tuple[str, dict]]:
    return [(svc, data) for svc, data in _climate_writes(engine) if data.get("hvac_mode") == "off"]


def _open_window(engine: AutomationEngine) -> None:
    engine._window_open = True
    _run(engine.handle_door_window_open(_WINDOW))


def _close_window(engine: AutomationEngine) -> None:
    engine._window_open = False
    _run(engine.handle_all_doors_windows_closed())


# ---------------------------------------------------------------------------
# #1133: a window that opens LATER must not pause HVAC over the occupant's change
# ---------------------------------------------------------------------------


class TestLaterWindowDoesNotPauseTheOccupantsChange:
    @pytest.mark.parametrize("mode", ["heat", "cool"])
    @pytest.mark.parametrize("grace_s", [0, 1800], ids=["grace0", "grace30m"])
    @pytest.mark.parametrize("confirm_s", [0, 600], ids=["confirmed", "pending"])
    def test_hvac_stays_as_the_occupant_set_it(self, timers, mode, grace_s, confirm_s):
        engine = _engine(
            mode,
            day="warm" if mode == "cool" else "mild",
            manual_grace_seconds=grace_s,
            override_confirm_seconds=confirm_s,
        )
        _occupant_overrides(engine)
        assert engine._manual_change_protected()

        _open_window(engine)

        assert engine._paused_by_door is False, "no window pause may be recorded over the occupant's change"
        assert _off_writes(engine) == [], "HVAC must not be switched off over the occupant's change"
        assert engine._test_state.state == mode
        assert engine._manual_change_protected(), "opening a window does not end the occupant's override"

    def test_control_without_an_override_the_window_still_pauses(self, timers):
        engine = _engine("heat", manual_grace_seconds=0)
        assert not engine._manual_change_protected()

        _open_window(engine)

        assert engine._paused_by_door is True
        assert engine._test_state.state == "off"

    def test_control_an_automation_grace_without_an_override_does_not_block_the_pause(self, timers):
        """Owner decision D16: a grace with no manual override behind it never protects anything here."""
        engine = _engine("heat", manual_grace_seconds=0)
        engine._start_grace_period("automation", trigger="sensor_closed_resume")
        assert engine._grace_active and not engine._manual_change_protected()
        # make the nat-vent gate viable so the #655 bypass falls through the (override-less) grace
        engine._last_outdoor_temp = 60.0
        engine._test_state.attributes["current_temperature"] = 72.0

        _open_window(engine)

        assert engine._natural_vent_active is True, "an override-less grace lets the viable gate start nat-vent"
        assert engine._paused_by_door is False

    def test_the_override_still_ends_by_the_occupants_next_change_cancel_and_the_window_is_then_checked(self, timers):
        """Grace 0 + window opened later: the override ends through Cancel Override, and the very next scheduled
        routine's live re-check pauses for the still-open window."""
        engine = _engine("heat", manual_grace_seconds=0, override_confirm_seconds=0)
        _occupant_overrides(engine)
        _open_window(engine)
        assert engine._paused_by_door is False

        assert engine.cancel_override(reason="user_cancel_override")
        assert not engine._manual_change_protected()
        _run(engine._sync_paused_by_door_with_live_sensors())

        assert engine._paused_by_door is True
        assert engine._test_state.state == "off"


class TestGraceExpiryStillSwitchesOffForAnOpenWindow:
    """The designed moment to honour an open window is grace expiry; the guard must not remove it."""

    def test_window_opened_during_the_grace_pauses_when_the_grace_ends(self, timers):
        engine = _engine("heat", manual_grace_seconds=1800, override_confirm_seconds=0)
        _occupant_overrides(engine)
        assert engine._grace_active and engine._manual_override_active
        _open_window(engine)
        assert engine._paused_by_door is False and engine._test_state.state == "heat"

        engine._on_grace_expired("manual", 1800, False)  # the real expiry callback body

        assert not engine._manual_change_protected(), "the override is cleared BEFORE the re-pause is evaluated"
        _run(engine._re_pause_for_open_sensor())  # the task _on_grace_expired schedules

        assert engine._paused_by_door is True
        assert engine._test_state.state == "off"


# ---------------------------------------------------------------------------
# #1148: the #655 real-gate bypass must not fall through to a pause over a protected manual grace
# ---------------------------------------------------------------------------


class TestBypassDoesNotFallThroughToAPause:
    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_viable_natvent_gate_during_a_manual_grace_does_not_pause(self, timers, mode):
        """Seed 1007000194 shape: manual grace running and the air outside is nat-vent-viable, so the #655
        bypass fell through -- and the nat-vent FSM refuses while an override is active, so it always paused."""
        engine = _engine(
            mode,
            indoor=72.0,
            outdoor=60.0,
            setpoint=70.0,
            day="warm" if mode == "cool" else "mild",
            manual_grace_seconds=1800,
            override_confirm_seconds=0,
        )
        _occupant_overrides(engine)
        assert engine._grace_active and engine._grace_protects_override

        _open_window(engine)

        assert engine._paused_by_door is False
        assert _off_writes(engine) == []
        assert engine._test_state.state == mode
        assert engine._natural_vent_active is False, "nat-vent cannot start over an override (FSM refuses it)"

    def test_control_viable_gate_without_an_override_starts_natvent(self, timers):
        engine = _engine("heat", indoor=72.0, outdoor=60.0, setpoint=70.0, manual_grace_seconds=1800)
        engine.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        engine.config[CONF_FAN_ENTITY] = "switch.whf"
        assert not engine._manual_change_protected()

        _open_window(engine)

        assert engine._natural_vent_active is True
        assert engine._paused_by_door is False


# ---------------------------------------------------------------------------
# #1147: ending a nat-vent session must not write the pre-vent mode over the occupant's change
# ---------------------------------------------------------------------------


def _natvent_engine(**cfg) -> AutomationEngine:
    """Nat-vent session running: the thermostat was Cool before the session (snapshot), the occupant then set Off."""
    engine = _engine(
        "off",
        indoor=74.0,
        outdoor=62.0,
        setpoint=76.0,
        day="warm",
        window_open=True,
        manual_grace_seconds=1800,
        override_confirm_seconds=0,
        **cfg,
    )
    engine.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
    engine.config[CONF_FAN_ENTITY] = "switch.whf"
    engine._natural_vent_active = True
    engine._pre_fan_hvac_mode = "cool"
    engine._fan_active = False
    return engine


class TestNatVentExitKeepsTheOccupantsChange:
    @pytest.mark.parametrize("sensors_closed", [True, False], ids=["window_closed", "window_still_open"])
    def test_exit_writes_no_hvac_and_releases_the_whf_snapshot(self, timers, sensors_closed):
        engine = _natvent_engine()
        _occupant_overrides(engine, new_mode="off")
        assert engine._manual_change_protected()
        engine._window_open = not sensors_closed
        engine.hass.services.async_call.reset_mock()
        grace_end_before = engine._grace_end_time

        _run(engine._exit_nat_vent(reason="window closed"))

        assert [w for w in _climate_writes(engine) if w[0] in ("set_hvac_mode", "set_temperature")] == []
        assert engine._test_state.state == "off", "the occupant's Off must survive the session ending"
        assert engine._natural_vent_active is False
        assert engine._pre_fan_hvac_mode is None, "the WHF's hold on the thermostat must be released"
        assert engine._whf_owns_hvac() is False
        assert engine._paused_by_door is False, "no window pause over the occupant's change"
        assert engine._grace_end_time == grace_end_before and engine._last_resume_source == "manual"

    def test_control_without_an_override_the_pre_vent_mode_is_restored(self, timers):
        engine = _natvent_engine()
        assert not engine._manual_change_protected()
        engine._window_open = False

        _run(engine._exit_nat_vent(reason="window closed"))

        assert engine._test_state.state == "cool"
        assert engine._natural_vent_active is False


def _no_hvac_writes(engine: AutomationEngine) -> list:
    return [w for w in _climate_writes(engine) if w[0] in ("set_hvac_mode", "set_temperature")]


def _occupant_changes(engine: AutomationEngine, source: str) -> None:
    """The occupant's change during the session: Off (a mode change) or a setpoint nudge on the Off thermostat."""
    if source == "setpoint":
        engine._test_state.attributes["temperature"] = 79.0
        _occupant_overrides(engine, source="setpoint", new_mode="off", old_setpoint_f=76.0, new_setpoint_f=79.0)
    else:
        _occupant_overrides(engine, new_mode="off")


def _slow_loop(engine: AutomationEngine, *, indoor: float) -> None:
    """The REAL 30-minute entry point, with only the exit-confirmation debounce and the indoor read pinned."""
    with (
        patch.object(type(engine), "_confirm_nat_vent_exit", lambda self, r, n: True),
        patch.object(type(engine), "_get_indoor_temp_f", lambda self: indoor),
    ):
        _run(engine.check_natural_vent_conditions())


@pytest.mark.parametrize("source", ["off", "setpoint"])
@pytest.mark.parametrize("confirm_s", [0, 600], ids=["confirmed", "pending"])
class TestNatVentExitsThatBypassTheChokePointKeepTheOccupantsChange:
    """Mirror of #1147: the slow-loop comfort-floor and away-ceiling exits never went through _exit_nat_vent().

    ``source="setpoint"`` here is an override the ENGINE recorded (e.g. made before the fan session); a setpoint
    change made WHILE the whole-house fan owns HVAC never reaches the engine at all (see
    ``TestSetpointChangeWhileWhfOwnsHvacIsIgnored``).
    """

    def _session(self, confirm_s: int, source: str) -> AutomationEngine:
        engine = _natvent_engine()
        engine.config["override_confirm_seconds"] = confirm_s
        _occupant_changes(engine, source)
        assert engine._manual_change_protected()
        engine._window_open = False
        engine._last_outdoor_temp = 55.0
        engine.hass.services.async_call.reset_mock()
        return engine

    def _assert_left_alone(self, engine: AutomationEngine) -> None:
        assert _no_hvac_writes(engine) == [], _climate_writes(engine)
        assert engine._test_state.state == "off"
        assert engine._natural_vent_active is False, "the session still ends"
        assert engine._pre_fan_hvac_mode is None and engine._whf_owns_hvac() is False
        assert engine._last_resume_source != "automation", "no automation grace on top of the occupant's change"

    def test_slow_loop_comfort_floor_exit(self, timers, confirm_s, source):
        engine = self._session(confirm_s, source)
        engine._current_classification.hvac_mode = "heat"  # the floor exit would arm heat
        _slow_loop(engine, indoor=60.0)
        self._assert_left_alone(engine)

    def test_slow_loop_away_ceiling_exit(self, timers, confirm_s, source):
        engine = self._session(confirm_s, source)
        engine._occupancy_mode = "away"
        _slow_loop(engine, indoor=78.0)
        self._assert_left_alone(engine)

    def test_temp_check_away_ceiling_exit(self, timers, confirm_s, source):
        engine = self._session(confirm_s, source)
        engine._occupancy_mode = "away"
        engine._window_open = True  # the per-tick path force-closes a WHF session when no sensor is open
        with patch.object(type(engine), "_confirm_nat_vent_exit", lambda self, r, n: True):
            _run(engine.nat_vent_temperature_check(78.0, outdoor=55.0))
        self._assert_left_alone(engine)

    def test_any_fan_stop_that_would_restore_hvac(self, timers, confirm_s, source):
        """The shared write: _deactivate_fan(restore_hvac=True) is the one place a fan stop writes a mode back."""
        engine = self._session(confirm_s, source)
        _run(engine._deactivate_fan(reason="reconcile / bedtime / wake-up style stop"))
        assert _no_hvac_writes(engine) == []
        assert engine._pre_fan_hvac_mode is None, "suppression is still released"


@pytest.mark.parametrize("protected", [True, False], ids=["protected", "control"])
def test_economizer_off_does_not_rearm_cooling_over_the_occupants_change(timers, protected):
    engine = _engine(
        "off", indoor=74.0, setpoint=79.0, day="hot", manual_grace_seconds=1800, override_confirm_seconds=0
    )
    engine._economizer_active = True
    if protected:
        _occupant_overrides(engine, new_mode="off")
        assert engine._manual_change_protected()
    engine.hass.services.async_call.reset_mock()
    _run(engine._deactivate_economizer(outdoor_temp=60.0))
    assert (_no_hvac_writes(engine) == []) is protected


def test_control_the_same_slow_loop_exits_without_an_override_still_restore(timers):
    engine = _natvent_engine()
    engine._current_classification.hvac_mode = "heat"
    engine._window_open = False
    engine._last_outdoor_temp = 55.0
    assert not engine._manual_change_protected()
    _slow_loop(engine, indoor=60.0)
    assert engine._natural_vent_active is False
    assert _no_hvac_writes(engine) != [], "positive control: with no override the floor exit does restore heat"


class TestPendingChangeThatSelfResolvesAppliesTheWindowPauseNow:
    """F3: the window opened while the change was pending was skipped; self-resolving must not leave it unpaused."""

    def test_pending_then_window_then_self_resolve_pauses(self, timers):
        engine = _engine("heat", day="cold", manual_grace_seconds=1800, override_confirm_seconds=600)
        # the occupant flips to cool and straight back: at expiry mode == classification (heat) -> PATH B
        _occupant_overrides(engine, source="normal", new_mode="heat")
        assert engine._override_confirm_pending
        created = []
        engine.hass.async_create_task = MagicMock(side_effect=lambda coro: created.append(coro))
        _open_window(engine)
        assert engine._paused_by_door is False, "skipped while the change is pending"

        timers.fire(600)  # confirmation expires: self-resolved
        assert not engine._manual_change_protected()
        for coro in created:
            _run(coro)

        assert engine._paused_by_door is True
        assert engine._test_state.state == "off"

    def test_self_resolve_with_nothing_deferred_requests_no_reclassify(self, timers):
        engine = _engine("heat", day="cold", manual_grace_seconds=1800, override_confirm_seconds=600)
        engine.config["manual_grace_notify"] = False
        _occupant_overrides(engine, source="normal", new_mode="heat")
        created = []
        engine.hass.async_create_task = MagicMock(side_effect=lambda coro: created.append(coro))
        engine._reclassify_callback = MagicMock()
        timers.fire(600)
        for coro in created:
            _run(coro)
        engine._reclassify_callback.assert_not_called()

    def test_self_resolve_after_a_routine_waited_reclassifies_right_away(self, timers):
        engine = _engine("heat", day="cold", manual_grace_seconds=1800, override_confirm_seconds=600)
        engine.config["manual_grace_notify"] = False
        _occupant_overrides(engine, source="normal", new_mode="heat")
        assert engine._scheduled_routine_waits_for_pending_override("wake-up")  # a routine waited
        created = []
        engine.hass.async_create_task = MagicMock(side_effect=lambda coro: created.append(coro))
        engine._reclassify_callback = MagicMock()

        timers.fire(600)
        for coro in created:
            _run(coro)

        assert engine._paused_by_door is False
        engine._reclassify_callback.assert_called_once_with(reason="override_self_resolved")  # F-E: right away


# ---------------------------------------------------------------------------
# #1131: a setpoint change made while a door pause has the thermostat Off is the occupant's
# ---------------------------------------------------------------------------


def _paused_engine(**cfg) -> AutomationEngine:
    """Heating, then a door opened and CA paused HVAC (really: through handle_door_window_open)."""
    engine = _engine("heat", indoor=64.0, outdoor=90.0, setpoint=68.0, day="mild", **cfg)
    _open_window(engine)
    assert engine._paused_by_door and engine._pre_pause_mode == "heat" and engine._test_state.state == "off"
    return engine


def _occupant_raises_setpoint(engine: AutomationEngine, to_f: float) -> None:
    engine._test_state.attributes["temperature"] = to_f
    _occupant_overrides(engine, source="setpoint", new_mode="off", old_setpoint_f=68.0, new_setpoint_f=to_f)


class TestSetpointChangeDuringAPauseSurvivesTheDoorClosing:
    @pytest.mark.parametrize("confirm_s", [0, 600], ids=["confirmed", "pending"])
    def test_hvac_resumes_in_the_pre_pause_mode_at_the_occupants_setpoint(self, timers, confirm_s):
        engine = _paused_engine(manual_grace_seconds=1800, override_confirm_seconds=confirm_s)
        _occupant_raises_setpoint(engine, 75.0)
        assert engine._manual_change_protected()
        engine.hass.services.async_call.reset_mock()
        grace_end_before = engine._grace_end_time

        _close_window(engine)

        assert engine._test_state.state == "heat", "HVAC resumes in the pre-pause mode"
        assert engine._test_state.attributes["temperature"] == 75.0, "...at the OCCUPANT's setpoint, not CA's"
        assert [w for w in _climate_writes(engine) if w[0] == "set_temperature"] == []
        assert engine._paused_by_door is False
        assert engine._grace_end_time == grace_end_before, "a 5-minute automation grace must not replace theirs"

    def test_the_override_is_recorded_as_the_pre_pause_mode_not_the_pause_off(self, timers):
        """The thermostat reads Off only because CA paused it. Recorded as 'off', the door-close restore looked
        like CA reversing the occupant's value (the override_respected hit on seeds 3000032 / 1000016) and a
        later REAL change to Off would be swallowed as 'the override's own mode'."""
        engine = _paused_engine(manual_grace_seconds=1800, override_confirm_seconds=0)
        emitted = []
        engine._emit_event_callback = lambda name, data: emitted.append((name, data))
        _occupant_raises_setpoint(engine, 75.0)

        assert engine._manual_override_mode == "heat"
        detected = [d for n, d in emitted if n == "override_detected"] or [
            d for n, d in emitted if n == "override_confirmed"
        ]
        assert all(d.get("detected_mode", d.get("mode")) == "heat" for d in detected)
        _close_window(engine)
        assert engine._manual_override_mode == "heat"

    def test_hvac_that_was_already_off_before_the_pause_keeps_off_as_the_mode(self, timers):
        engine = _engine("off", manual_grace_seconds=1800, override_confirm_seconds=0)
        _open_window(engine)
        assert engine._paused_by_door and engine._pre_pause_mode is None
        _occupant_raises_setpoint(engine, 75.0)
        assert engine._manual_override_mode == "off"

    def test_pending_confirmation_confirms_in_the_restored_mode(self, timers):
        engine = _paused_engine(manual_grace_seconds=1800, override_confirm_seconds=600)
        _occupant_raises_setpoint(engine, 75.0)
        _close_window(engine)
        assert engine._override_confirm_pending and engine._override_confirm_mode == "heat"

        timers.fire(600)

        assert engine._manual_override_active and engine._manual_override_mode == "heat"
        assert engine._test_state.attributes["temperature"] == 75.0

    def test_control_without_an_override_the_close_restores_comfort_and_starts_the_automation_grace(self, timers):
        engine = _paused_engine(manual_grace_seconds=1800)
        assert not engine._manual_change_protected()
        engine.hass.services.async_call.reset_mock()

        _close_window(engine)

        assert engine._test_state.state == "heat"
        assert engine._last_resume_source == "automation"


# ---------------------------------------------------------------------------
# #1146: the confirm source must reach the FSM (cleared-before-read)
# ---------------------------------------------------------------------------


class TestSetpointOnlyOverrideLandsProtecting:
    def test_setpoint_override_with_a_matching_mode_gets_its_grace_flags(self, timers):
        """Confirm 600 s, mode == classification (heat on a heat day): PATH A must run."""
        engine = _engine("heat", day="cold", manual_grace_seconds=1800, override_confirm_seconds=600)
        assert engine._current_classification.hvac_mode == "heat"
        engine._test_state.attributes["temperature"] = 72.0
        _occupant_overrides(engine, source="setpoint", new_mode="heat", old_setpoint_f=68.0, new_setpoint_f=72.0)
        assert engine._override_confirm_pending

        timers.fire(600)

        assert engine._manual_override_active is True
        assert engine._grace_active is True
        assert engine._grace_protects_override is True
        assert engine.override_grace_lifecycle_state == _PROTECTING
        assert engine._override_confirm_source is None, "the confirm bookkeeping is still cleared"

    def test_control_a_mode_change_that_reverted_still_self_resolves(self, timers):
        engine = _engine("heat", day="cold", manual_grace_seconds=1800, override_confirm_seconds=600)
        _occupant_overrides(engine, source="normal", new_mode="heat")
        timers.fire(600)

        assert engine._manual_override_active is False
        assert engine.override_grace_lifecycle_state == _NOTHING

    def test_the_builder_takes_the_source_explicitly(self, timers):
        engine = _engine("heat", day="cold")
        assert engine._override_confirm_source is None
        assert engine._build_override_grace_fsm_inputs().setpoint_override is False
        assert engine._build_override_grace_fsm_inputs(confirm_source="setpoint").setpoint_override is True

    def test_a_window_opening_after_the_confirmed_setpoint_change_does_not_pause(self, timers):
        engine = _engine("heat", day="cold", manual_grace_seconds=1800, override_confirm_seconds=600)
        engine._test_state.attributes["temperature"] = 72.0
        _occupant_overrides(engine, source="setpoint", new_mode="heat", old_setpoint_f=68.0, new_setpoint_f=72.0)
        timers.fire(600)

        _open_window(engine)

        assert engine._paused_by_door is False
        assert _off_writes(engine) == []


# ---------------------------------------------------------------------------
# #1152: the dead coordinator copy is gone
# ---------------------------------------------------------------------------


class TestDeadCoordinatorCopyIsGone:
    def test_nothing_of_the_coordinator_tracker_remains(self):
        import importlib

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        cls = mod.ClimateAdvisorCoordinator
        for name in (
            "_evaluate_override_grace_fsm",
            "_feed_override_grace_fsm_on_detect",
            "_feed_override_grace_fsm_cancelled",
            "_feed_override_grace_fsm_if_cleared",
            "_any_override_active",
            "_feed_lifecycle_fsms_from_event",
        ):
            assert not hasattr(cls, name), name
        assert not hasattr(mod, "_OVERRIDE_GRACE_FSM_EVENT_KINDS")
        assert not hasattr(mod, "_OVERRIDE_GRACE_FSM_EVENT_TYPE_MAP")


# ---------------------------------------------------------------------------
# Owner principle (2026-10-31): the latest manual change creates the latest override and REPLACES the old one.
# An occupant fan press replaces any thermostat change; when the fan session ends CA restores what the fan paused
# and resumes its own plan -- nothing earlier is ever revived as an override.
# ---------------------------------------------------------------------------


def _occupant_heat_then_fan_press(*, grace_s: int, confirmed: bool = True) -> tuple[AutomationEngine, list]:
    """The occupant sets heat 74 FIRST (confirmed or pending); later turns the WHF on (the real entry point)."""
    engine = _engine(
        "heat",
        indoor=60.0,
        setpoint=68.0,
        day="cold",
        outdoor=50.0,
        window_open=True,
        manual_grace_seconds=grace_s,
        override_confirm_seconds=0 if confirmed else 600,
    )
    engine.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
    engine.config[CONF_FAN_ENTITY] = "switch.whf"
    tasks: list = []
    engine.hass.async_create_task = MagicMock(side_effect=lambda coro: tasks.append(coro))
    engine._test_state.attributes["temperature"] = 74.0
    _occupant_overrides(engine, source="normal", new_mode="heat")
    assert engine._manual_change_protected()
    engine.handle_fan_manual_override(fan_before="off", fan_after="on")
    engine._fan_active = True
    for c in list(tasks):
        _run(c)
    tasks.clear()
    engine.hass.services.async_call.reset_mock()
    return engine, tasks


@pytest.mark.parametrize("grace_s", [0, 1800], ids=["grace0", "grace30m"])
class TestFanSessionEndRestoresThePreFanModeAndResumesCaPlan:
    def _pressed(self, grace_s):
        engine, tasks = _occupant_heat_then_fan_press(grace_s=grace_s)
        assert engine._manual_change_protected() is False, "the fan press replaced the thermostat change"
        assert engine._test_state.state == "off" and engine._pre_fan_hvac_mode == "heat", "the fan paused the heat"
        return engine, tasks

    def test_the_press_replaces_a_confirmed_change_at_any_grace(self, timers, grace_s):
        engine, _tasks = self._pressed(grace_s)
        assert engine._manual_override_active is False and engine._manual_override_mode is None
        expected = _PROTECTING if grace_s else _NOTHING
        assert engine.override_grace_lifecycle_state == expected

    def test_sealed_house_stop_restores_what_the_fan_paused(self, timers, grace_s):
        engine, _tasks = self._pressed(grace_s)
        engine._window_open = False
        engine.hass.async_create_task = MagicMock(
            side_effect=lambda coro: coro.close()
        )  # the stop's notify and cycle restart
        # Issue #1144: the sealed-house stop is the WHF safety enforcer (the old event-driven guard is gone).
        _run(engine.enforce_whf_safety("sealed", sealed_confirmed=True))
        assert engine._test_state.state == "heat", "the normal pre-fan restore ran"
        assert engine._pre_fan_hvac_mode is None and engine._whf_owns_hvac() is False
        assert engine._manual_change_protected() is False, "no earlier change was revived as an override"

    def test_occupant_turning_the_fan_off_releases_and_reclassifies_with_no_override(self, timers, grace_s):
        engine, tasks = self._pressed(grace_s)
        engine._fan_active = False
        engine._reclassify_callback = MagicMock()
        with patch(f"{_AUTOMATION}.dt_util.parse_datetime", side_effect=datetime.fromisoformat):
            engine.on_fan_turned_off(fan_before="on", fan_after="off")
        for c in list(tasks):
            _run(c)
        assert engine._pre_fan_hvac_mode is None and engine._whf_owns_hvac() is False
        engine._reclassify_callback.assert_called_once_with()  # CA's own plan resumes through the reclassify
        assert not hasattr(engine, "_whf_release_restore_mode"), "no hand-back stash exists any more"
        assert engine._manual_change_protected() is False

        # the fan-off cooldown ends, then CA's own scheduled state is applied (what the grace callback runs)
        engine._window_open = False  # the window is shut (with it open, rule 1 keeps HVAC off)
        engine._on_grace_expired("automation", 300, False)
        _run(engine._apply_current_scheduled_state())
        assert engine._test_state.state == "heat", "CA's plan on a cold day is heat"
        assert engine._manual_change_protected() is False


@pytest.mark.parametrize("grace_s", [0, 1800], ids=["grace0", "grace30m"])
def test_a_pending_change_is_replaced_by_a_fan_press_and_never_confirms(timers, grace_s):
    engine, tasks = _occupant_heat_then_fan_press(grace_s=grace_s, confirmed=False)
    assert engine._override_confirm_pending is False, "dropped, at grace 0 as at grace > 0"
    assert engine._override_confirm_cancel is None, "the confirmation timer was cancelled"
    assert engine._override_confirm_mode is None and engine._override_confirm_time is None
    timers.fire(600)  # the (cancelled) confirmation timer, if it were to fire
    for c in list(tasks):
        _run(c)
    assert engine._manual_override_active is False and engine._manual_override_mode is None
    assert engine._manual_change_protected() is False


class TestOwnFanStartsRefuseAProtectedActiveMode:
    @pytest.mark.parametrize("confirm_s", [0, 600], ids=["confirmed", "pending"])
    def test_activate_fan_refuses(self, timers, confirm_s):
        """F-D: CA's own min-runtime / economizer start must not suppress the occupant's (even pending) heat."""
        engine = _engine("heat", day="cold", manual_grace_seconds=1800, override_confirm_seconds=confirm_s)
        engine.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        engine.config[CONF_FAN_ENTITY] = "switch.whf"
        _occupant_overrides(engine, new_mode="heat")
        assert engine._manual_change_protected()
        engine.hass.services.async_call.reset_mock()

        result = _run(engine._activate_fan(reason="min-runtime cycle"))

        assert result.name == "OVERRIDDEN"
        assert engine._test_state.state == "heat" and engine._pre_fan_hvac_mode is None
        assert engine.hass.services.async_call.call_args_list == []

    def test_an_occupant_off_is_not_an_active_mode(self, timers):
        """Off is not an active mode: there is nothing for the entry guard to protect from a fan start."""
        engine = _natvent_engine()
        _occupant_overrides(engine, new_mode="off")
        assert engine._protected_active_mode() is None


# ---------------------------------------------------------------------------
# Owner decision 2026-10-31 (reverses F-C): a temperature-only change while the WHF owns HVAC is IGNORED
# ---------------------------------------------------------------------------


def _whf_session(*, pre_fan: str | None, day: str = "cold", confirm_s: int = 0) -> tuple[AutomationEngine, list]:
    engine = _engine(
        "off",
        indoor=66.0,
        setpoint=68.0,
        day=day,
        outdoor=55.0,
        window_open=True,
        manual_grace_seconds=1800,
        override_confirm_seconds=confirm_s,
    )
    engine.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
    engine.config[CONF_FAN_ENTITY] = "switch.whf"
    engine._pre_fan_hvac_mode = pre_fan
    engine._fan_active = True
    engine._natural_vent_active = True
    tasks: list = []
    engine.hass.async_create_task = MagicMock(side_effect=lambda coro: tasks.append(coro))
    engine.hass.services.async_call.reset_mock()
    return engine, tasks


def test_mode_change_during_a_fan_session_is_unchanged_714(timers):
    """Existing #714/#748 behaviour: the occupant picks the mode; the stand-down ends the fan session."""
    engine, tasks = _whf_session(pre_fan="cool")
    engine._test_state.state = "heat"  # the occupant chose heat at the thermostat
    _occupant_overrides(engine, new_mode="heat")
    for coro in tasks:
        _run(coro)
    assert engine._natural_vent_active is False
    assert engine._test_state.state == "heat"
    assert [w for w in _climate_writes(engine) if w[0] == "set_hvac_mode"] == []


def _coordinator_setpoint_event(*, whf_owns: bool, new_temp):
    """The REAL coordinator ``_async_thermostat_changed`` fed a temperature-only change on an Off thermostat."""
    import asyncio

    from tests.test_heat_cool_override import (
        _NOW_BASE,
        _PATCH_CALLBACK,
        _PATCH_DT_UTIL,
        _make_coordinator_stub,
        _make_event,
    )

    coord = _make_coordinator_stub(last_commanded_hvac_mode="off")
    coord.automation_engine._whf_owns_hvac = MagicMock(return_value=whf_owns)
    event = _make_event(old_hvac_mode="off", new_hvac_mode="off", old_temp=68.0, new_temp=new_temp)
    mock_dt = MagicMock()
    mock_dt.now.return_value = _NOW_BASE
    with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
        asyncio.run(coord._async_thermostat_changed(event))
    return coord


class TestSetpointChangeWhileWhfOwnsHvacIsIgnored:
    """The thermostat reads Off only because the fan suppressed HVAC, and a window is open: heating then would heat
    an open window (rule 1/3, #1131). Only a mode change or the fan turning off ends the session."""

    def test_temperature_only_change_is_not_an_override_and_stops_nothing(self, caplog):
        import logging

        with caplog.at_level(logging.INFO):
            coord = _coordinator_setpoint_event(whf_owns=True, new_temp=74.0)
        ae = coord.automation_engine
        assert ae.handle_manual_override.call_count == 0
        assert coord._today_record.manual_overrides == 0, "not even counted as an override"
        assert ae._stand_down_whf_for_override_conflict.call_count == 0
        assert any("Setpoint change ignored: whole-house fan owns HVAC" in r.getMessage() for r in caplog.records)

    def test_control_without_the_fan_the_same_change_is_an_override(self):
        coord = _coordinator_setpoint_event(whf_owns=False, new_temp=74.0)
        assert coord.automation_engine.handle_manual_override.call_count == 1
        assert coord._today_record.manual_overrides == 1


class TestNonNumericSetpointRegistersNothing:
    """#583: a non-numeric setpoint pair is not an override (it used to still call handle_manual_override)."""

    def test_non_numeric_reading_is_not_registered(self):
        coord = _coordinator_setpoint_event(whf_owns=False, new_temp="n/a")
        assert coord.automation_engine.handle_manual_override.call_count == 0
        assert coord._today_record.manual_overrides == 0

    def test_control_missing_prior_setpoint_with_a_readable_new_one_is_still_an_override(self):
        """Sensi reports temperature=null while Off; the occupant then sets one: a real change (#1049 contract)."""
        import asyncio

        from tests.test_heat_cool_override import (
            _NOW_BASE,
            _PATCH_CALLBACK,
            _PATCH_DT_UTIL,
            _make_coordinator_stub,
            _make_event,
        )

        coord = _make_coordinator_stub(last_commanded_hvac_mode="cool")
        event = _make_event(old_hvac_mode="cool", new_hvac_mode="cool", new_temp=72.0)  # no old temperature
        mock_dt = MagicMock()
        mock_dt.now.return_value = _NOW_BASE
        with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
            asyncio.run(coord._async_thermostat_changed(event))
        assert coord.automation_engine.handle_manual_override.call_count == 1

    def test_control_numeric_reading_is_registered(self):
        coord = _coordinator_setpoint_event(whf_owns=False, new_temp=72.0)
        assert coord.automation_engine.handle_manual_override.call_count == 1


# ---------------------------------------------------------------------------
# Post-review fixes: verify timers, notify isolation, the F-E deferral flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["fan_on", "fan_off"])
def test_the_30s_setpoint_verify_does_not_reassert_over_a_pending_change(timers, which):
    """Both post-fan verify timers got ``manual_override_active=self._manual_override_active`` -- a change made
    after the fan command but still PENDING was reasserted over at 30 s. They now use the shared predicate."""
    engine = _engine("heat", day="cold", manual_grace_seconds=1800, override_confirm_seconds=600, window_open=True)
    engine.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
    engine.config[CONF_FAN_ENTITY] = "switch.whf"
    engine._pending_setpoint_single = 68.0  # what CA last commanded: the verify timer would restore this
    engine._last_commanded_hvac_mode = "heat"
    if which == "fan_on":
        _run(engine._activate_fan(reason="min-runtime cycle"))  # no override yet: CA starts the fan
    else:
        engine._fan_active = True
        _run(engine._deactivate_fan(reason="stop", restore_hvac=False, release_suppression=False))
    verify = [fn for d, fn in timers.pending if d == 30.0]
    assert verify, "the fan command armed a 30 s setpoint verify"
    engine._test_state.attributes["temperature"] = 74.0  # the occupant's new value ...
    _occupant_overrides(engine, source="normal", new_mode="heat")  # ... which is only PENDING confirmation
    assert engine._override_confirm_pending and not engine._manual_override_active
    engine.hass.services.async_call.reset_mock()
    engine.hass.async_create_task = MagicMock(side_effect=lambda coro: _run(coro))

    verify[-1](None)

    assert [w for w in _climate_writes(engine) if w[0] == "set_temperature"] == [], "no reassert over the change"
    assert engine._test_state.attributes["temperature"] == 74.0


def test_a_failing_notification_never_skips_the_window_recheck(timers):
    """Rule 1: the re-check/pause comes BEFORE the notification, and a failing notify service (the REAL
    ``_notify``, whose hass call raises) can neither skip it nor raise out of the task."""
    engine = _engine("heat", day="cold", manual_grace_seconds=1800, override_confirm_seconds=600)
    _occupant_overrides(engine, source="normal", new_mode="heat")
    _open_window(engine)
    assert engine._paused_by_door is False
    engine._override_confirm_pending = False  # it self-resolved
    _notify_raises(engine)
    _run(engine._after_override_self_resolved(notify=True, recheck_window=True))
    assert engine._paused_by_door is True, "the pause was applied even though the notification raised"


class TestDeferredWhilePendingFlag:
    def _pending(self, timers):
        engine = _engine("heat", day="cold", manual_grace_seconds=1800, override_confirm_seconds=600)
        _occupant_overrides(engine, source="normal", new_mode="heat")
        assert engine._override_confirm_pending
        return engine

    def test_set_by_apply_classification_waiting(self, timers):
        engine = self._pending(timers)
        _run(engine.apply_classification(engine._current_classification))
        assert engine._deferred_while_pending is True

    def test_set_by_tou_precondition_waiting(self, timers):
        engine = self._pending(timers)
        _run(engine.apply_tou_precondition(engine._current_classification, 70.0, "tou-1"))
        assert engine._deferred_while_pending is True

    def test_set_by_away_vacation_tou_waiting(self, timers):
        engine = self._pending(timers)
        _run(engine.apply_tou_away_vacation_precondition(engine._current_classification, 70.0, "tou-1", "heat"))
        assert engine._deferred_while_pending is True

    def test_set_by_the_arrival_hold(self, timers):
        import asyncio

        from tests.test_occupancy_restore_hold import _engine as _occupancy_engine

        eng, events, _call = _occupancy_engine()
        eng._override_confirm_pending = True  # a change is awaiting confirmation when the occupant arrives home
        asyncio.run(eng.handle_occupancy_home())
        assert any(e == "occupancy_restore_deferred" for e, _ in events)
        assert eng._deferred_while_pending is True

    def test_set_by_a_routine_wait_and_by_a_skipped_window(self, timers):
        engine = self._pending(timers)
        assert engine._scheduled_routine_waits_for_pending_override("wake-up")
        assert engine._deferred_while_pending is True
        engine._deferred_while_pending = False
        _open_window(engine)
        assert engine._deferred_while_pending is True

    def test_cleared_on_confirm(self, timers):
        engine = _engine("cool", day="cold", manual_grace_seconds=1800, override_confirm_seconds=600)
        _occupant_overrides(engine, source="normal", new_mode="cool")  # divergent from the heat day -> confirms
        engine._deferred_while_pending = True
        timers.fire(600)
        assert engine._manual_override_active
        assert engine._deferred_while_pending is False

    def test_cleared_on_cancel(self, timers):
        engine = _engine("heat", day="cold", manual_grace_seconds=1800, override_confirm_seconds=0)
        _occupant_overrides(engine, source="normal", new_mode="heat")
        engine._deferred_while_pending = True
        assert engine.cancel_override(reason="user_cancel_override")
        assert engine._deferred_while_pending is False


# ---------------------------------------------------------------------------
# A change pending BEFORE the occupant runs the whole-house fan (real entry points, hot and mild days)
# ---------------------------------------------------------------------------


def _pending_then_whf(timers, *, day: str, source: str, grace_s: int):
    """Occupant changes the thermostat (pending), then turns the WHF on through the real entry point."""
    engine = _engine(
        "heat",
        day=day,
        indoor=66.0,
        setpoint=70.0,
        outdoor=55.0,
        window_open=True,
        manual_grace_seconds=grace_s,
        override_confirm_seconds=600,
    )
    engine.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
    engine.config[CONF_FAN_ENTITY] = "switch.whf"
    tasks: list = []
    engine.hass.async_create_task = MagicMock(side_effect=lambda coro: tasks.append(coro))
    engine._last_commanded_hvac_mode = engine._current_classification.hvac_mode
    if source == "setpoint":
        engine._test_state.attributes["temperature"] = 74.0
        _occupant_overrides(engine, source="setpoint", new_mode="heat", old_setpoint_f=70.0, new_setpoint_f=74.0)
    else:
        _occupant_overrides(engine, source="normal", new_mode="heat")
    assert engine._override_confirm_pending and engine._override_confirm_mode == "heat"
    engine.handle_fan_manual_override(fan_before="off", fan_after="on")
    engine._fan_active = True
    for c in list(tasks):
        _run(c)
    tasks.clear()
    assert engine._test_state.state == "off" and engine._pre_fan_hvac_mode == "heat", "the fan suppressed HVAC"
    return engine, tasks


@pytest.mark.parametrize("grace_s", [0, 1800], ids=["grace0", "grace30m"])
@pytest.mark.parametrize("source", ["normal", "setpoint"])
@pytest.mark.parametrize("day", ["hot", "mild"])
class TestPendingChangeIsDroppedByAFanPress:
    """It used to be confirmed against the fan's Off: recorded as ``off`` on a hot day (blocking every reclassify
    indefinitely) and discarded as a transient on a mild day. Now it is simply replaced -- at any grace setting."""

    def test_dropped_not_confirmed_as_off(self, timers, day, source, grace_s):
        engine, tasks = _pending_then_whf(timers, day=day, source=source, grace_s=grace_s)
        assert engine._override_confirm_pending is False
        assert engine._override_confirm_cancel is None, "the confirmation timer was cancelled"
        assert engine._override_confirm_mode is None and engine._override_confirm_time is None

        timers.fire(600)
        for c in list(tasks):
            _run(c)
        tasks.clear()

        assert engine._manual_override_active is False, "never confirmed -- not as 'off' (hot day) either"
        assert engine._manual_override_mode is None
        assert engine._manual_change_protected() is False, "nothing blocks the reclassify"

    def test_the_fans_end_resumes_cas_plan_with_no_override(self, timers, day, source, grace_s):
        engine, tasks = _pending_then_whf(timers, day=day, source=source, grace_s=grace_s)
        engine._fan_active = False
        engine._reclassify_callback = MagicMock()
        with patch(f"{_AUTOMATION}.dt_util.parse_datetime", side_effect=datetime.fromisoformat):
            engine.on_fan_turned_off(fan_before="on", fan_after="off")
        for c in list(tasks):
            _run(c)
        assert engine._pre_fan_hvac_mode is None
        engine._reclassify_callback.assert_called_once_with()
        assert engine._manual_change_protected() is False


# ---------------------------------------------------------------------------
# M3: _notify never raises
# ---------------------------------------------------------------------------


def _notify_raises(engine: AutomationEngine) -> None:
    real = engine.hass.services.async_call.side_effect

    async def _call(domain, service, data, **kw):
        if domain == "notify":
            raise RuntimeError("notify service down")
        return await real(domain, service, data, **kw)

    engine.hass.services.async_call.side_effect = _call


def test_notify_never_raises_and_warns(timers, caplog):
    import logging

    engine = _engine("heat", day="cold")
    _notify_raises(engine)
    with caplog.at_level(logging.WARNING):
        _run(engine._notify("m", "Climate Advisor", notification_type="door_window_pause"))
    assert any("Notification failed (push)" in r.getMessage() for r in caplog.records)


def test_door_pause_is_recorded_and_restored_even_when_notify_raises(timers):
    """#1132 class: a failing notify after set_hvac_mode(off) and before the pause was recorded left HVAC off."""
    engine = _engine("heat", day="cold", manual_grace_seconds=1800)
    _notify_raises(engine)
    _open_window(engine)
    assert engine._test_state.state == "off"
    assert engine._paused_by_door is True, "the pause is recorded although the notification failed"
    _close_window(engine)
    assert engine._test_state.state == "heat", "closing the window restores HVAC"


# ---------------------------------------------------------------------------
# L1 / L2: the deferral flag
# ---------------------------------------------------------------------------


class TestDeferralFlagLifecycle:
    def _pending(self):
        engine = _engine("heat", day="cold", manual_grace_seconds=1800, override_confirm_seconds=600)
        _occupant_overrides(engine, source="normal", new_mode="heat")
        assert engine._override_confirm_pending
        return engine

    def test_cleared_by_clear_manual_override(self, timers):
        engine = self._pending()
        engine._deferred_while_pending = True
        engine.clear_manual_override(reason="occupancy_away")
        assert engine._deferred_while_pending is False

    def test_cleared_by_grace_expiry(self, timers):
        engine = _engine("heat", day="cold", manual_grace_seconds=1800, override_confirm_seconds=0)
        _occupant_overrides(engine, source="normal", new_mode="heat")
        engine._deferred_while_pending = True
        engine._on_grace_expired("manual", 1800, False)
        assert engine._deferred_while_pending is False

    def test_set_by_the_protected_nat_vent_exit(self, timers):
        engine = _natvent_engine()
        engine.config["override_confirm_seconds"] = 600
        _occupant_overrides(engine, new_mode="off")
        assert engine._override_confirm_pending
        engine._window_open = False
        _run(engine._exit_nat_vent(reason="window closed"))
        assert engine._deferred_while_pending is True

    def test_set_by_the_protected_economizer_off(self, timers):
        engine = _engine(
            "off", indoor=74.0, setpoint=79.0, day="hot", manual_grace_seconds=1800, override_confirm_seconds=600
        )
        engine._economizer_active = True
        _occupant_overrides(engine, new_mode="off")
        assert engine._override_confirm_pending
        _run(engine._deactivate_economizer(outdoor_temp=60.0))
        assert engine._deferred_while_pending is True

    def test_set_by_the_protected_comfort_floor_exit(self, timers):
        engine = _natvent_engine()
        engine.config["override_confirm_seconds"] = 600
        _occupant_overrides(engine, new_mode="off")
        engine._current_classification.hvac_mode = "heat"
        engine._window_open = False
        engine._last_outdoor_temp = 55.0
        _slow_loop(engine, indoor=60.0)
        assert engine._deferred_while_pending is True

    def test_set_by_the_protected_door_close_restore(self, timers):
        engine = _paused_engine(manual_grace_seconds=1800, override_confirm_seconds=600)
        _occupant_raises_setpoint(engine, 75.0)
        assert engine._override_confirm_pending
        _close_window(engine)
        assert engine._deferred_while_pending is True


# ---------------------------------------------------------------------------
# L6: CA's own restore echo with a missing prior setpoint
# ---------------------------------------------------------------------------


def _coordinator_missing_prior(*, cmd_age_s):
    import asyncio
    from datetime import timedelta

    from tests.test_heat_cool_override import (
        _NOW_BASE,
        _PATCH_CALLBACK,
        _PATCH_DT_UTIL,
        _make_coordinator_stub,
        _make_event,
    )

    coord = _make_coordinator_stub(
        last_commanded_hvac_mode="cool",
        last_commanded_hvac_time=None if cmd_age_s is None else _NOW_BASE - timedelta(seconds=cmd_age_s),
    )
    event = _make_event(old_hvac_mode="cool", new_hvac_mode="cool", new_temp=72.0)  # no prior setpoint
    mock_dt = MagicMock()
    mock_dt.now.return_value = _NOW_BASE
    with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
        asyncio.run(coord._async_thermostat_changed(event))
    return coord


class TestLateCaEchoWithMissingPriorSetpoint:
    def test_echo_of_ca_own_command_at_60s_is_not_an_override(self):
        coord = _coordinator_missing_prior(cmd_age_s=60)
        assert coord.automation_engine.handle_manual_override.call_count == 0

    def test_control_no_recent_ca_command_is_still_an_override(self):
        coord = _coordinator_missing_prior(cmd_age_s=None)
        assert coord.automation_engine.handle_manual_override.call_count == 1

    def test_control_command_older_than_120s_is_still_an_override(self):
        coord = _coordinator_missing_prior(cmd_age_s=300)
        assert coord.automation_engine.handle_manual_override.call_count == 1


# ---------------------------------------------------------------------------
# The replacement rule is for the WHOLE-HOUSE fan only (WHF and HVAC fan are different physics)
# ---------------------------------------------------------------------------


def _heat_change_then_press(*, fan_mode: str, confirmed: bool, grace_s: int, hvac_fan: bool):
    from custom_components.climate_advisor.const import CONF_FAN_MODE

    engine = _engine(
        "heat",
        indoor=60.0,
        setpoint=68.0,
        day="cold",
        outdoor=50.0,
        manual_grace_seconds=grace_s,
        override_confirm_seconds=0 if confirmed else 600,
    )
    engine.config[CONF_FAN_MODE] = fan_mode
    engine.config[CONF_FAN_ENTITY] = "switch.whf"
    tasks: list = []
    engine.hass.async_create_task = MagicMock(side_effect=lambda coro: tasks.append(coro))
    engine._test_state.attributes["temperature"] = 74.0  # the occupant's heat 74
    _occupant_overrides(engine, source="normal", new_mode="heat")
    assert engine._manual_change_protected()
    engine.handle_fan_manual_override(fan_before="auto", fan_after="on", hvac_fan=hvac_fan)
    for c in tasks:
        c.close()  # not run: the (pre-existing) HVAC suppression task is not under test here
    engine.hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())  # later tasks: closed too
    engine.hass.services.async_call.reset_mock()
    return engine


class TestOnlyAWholeHouseFanPressReplacesAThermostatChange:
    @pytest.mark.parametrize("fan_mode", ["hvac_fan", "both", "whole_house_fan"])
    @pytest.mark.parametrize("grace_s", [0, 1800], ids=["grace0", "grace30m"])
    def test_a_thermostat_fan_press_keeps_a_confirmed_heat_change(self, timers, fan_mode, grace_s):
        """The occupant sets heat 74 and switches the THERMOSTAT fan on for circulation: CA must not write 68."""
        engine = _heat_change_then_press(fan_mode=fan_mode, confirmed=True, grace_s=grace_s, hvac_fan=True)
        assert engine._manual_override_active is True and engine._manual_override_mode == "heat"
        _run(engine.apply_classification(engine._current_classification))
        assert [w for w in _climate_writes(engine) if w[0] == "set_temperature"] == []
        assert engine._test_state.attributes["temperature"] == 74.0

    @pytest.mark.parametrize("fan_mode", ["hvac_fan", "both", "whole_house_fan"])
    @pytest.mark.parametrize("grace_s", [0, 1800], ids=["grace0", "grace30m"])
    def test_a_thermostat_fan_press_keeps_a_pending_heat_change(self, timers, fan_mode, grace_s):
        engine = _heat_change_then_press(fan_mode=fan_mode, confirmed=False, grace_s=grace_s, hvac_fan=True)
        assert engine._override_confirm_pending is True, "still pending at any grace"
        assert engine._override_confirm_cancel is not None, "its confirmation timer is intact (not orphaned)"
        assert engine._override_confirm_mode == "heat"
        _run(engine.apply_classification(engine._current_classification))
        assert [w for w in _climate_writes(engine) if w[0] == "set_temperature"] == []
        assert engine._test_state.attributes["temperature"] == 74.0

    @pytest.mark.parametrize("fan_mode", ["both", "whole_house_fan"])
    @pytest.mark.parametrize("confirmed", [True, False], ids=["confirmed", "pending"])
    def test_a_whole_house_fan_press_replaces_it(self, timers, fan_mode, confirmed):
        engine = _heat_change_then_press(fan_mode=fan_mode, confirmed=confirmed, grace_s=0, hvac_fan=False)
        assert engine._manual_change_protected() is False

    def test_both_the_thermostat_leg_keeps_it_and_the_whf_leg_replaces_it(self, timers):
        engine = _heat_change_then_press(fan_mode="both", confirmed=True, grace_s=0, hvac_fan=True)
        assert engine._manual_change_protected() is True, "thermostat-fan leg of BOTH"
        engine.handle_fan_manual_override(fan_before="off", fan_after="on", hvac_fan=False)
        assert engine._manual_change_protected() is False, "whole-house-fan leg of BOTH"

    def test_with_fan_mode_hvac_fan_a_whole_house_style_press_cannot_replace_it(self, timers):
        """No WHF is configured, so there is no WHF session for the press to start or continue."""
        engine = _heat_change_then_press(fan_mode="hvac_fan", confirmed=True, grace_s=0, hvac_fan=False)
        assert engine._manual_change_protected() is True


def test_one_thermostat_event_with_a_mode_change_and_a_fan_mode_change_keeps_the_mode_override():
    """heat->cool plus fan auto->on in ONE event: the occupant's cool is the root cause; the fan_mode block must not
    run for the same event (the setpoint case was already guarded)."""
    import asyncio

    from tests.test_heat_cool_override import (
        _NOW_BASE,
        _PATCH_CALLBACK,
        _PATCH_DT_UTIL,
        _make_coordinator_stub,
        _make_event,
    )

    coord = _make_coordinator_stub(last_commanded_hvac_mode="heat")
    coord.automation_engine._whf_owns_hvac = MagicMock(return_value=False)
    event = _make_event(
        old_hvac_mode="heat", new_hvac_mode="cool", old_temp=68.0, new_temp=68.0, old_fan_mode="auto", new_fan_mode="on"
    )
    mock_dt = MagicMock()
    mock_dt.now.return_value = _NOW_BASE
    with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
        asyncio.run(coord._async_thermostat_changed(event))
    ae = coord.automation_engine
    assert ae.handle_manual_override.call_count == 1, "the cool change is registered"
    assert ae.handle_fan_manual_override.call_count == 0, "...and the fan_mode block did not replace it"


def test_a_thermostat_fan_mode_change_alone_is_an_hvac_fan_press():
    """The coordinator's thermostat fan_mode block is the HVAC-fan leg: it passes ``hvac_fan=True``."""
    import asyncio

    from tests.test_heat_cool_override import (
        _NOW_BASE,
        _PATCH_CALLBACK,
        _PATCH_DT_UTIL,
        _make_coordinator_stub,
        _make_event,
    )

    coord = _make_coordinator_stub()
    event = _make_event(old_hvac_mode="heat", new_hvac_mode="heat", old_fan_mode="auto", new_fan_mode="on")
    mock_dt = MagicMock()
    mock_dt.now.return_value = _NOW_BASE
    with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
        asyncio.run(coord._async_thermostat_changed(event))
    call = coord.automation_engine.handle_fan_manual_override.call_args
    assert call is not None and call.kwargs.get("hvac_fan") is True


def test_the_shared_replace_helper_for_mode_to_mode_supersession_keeps_the_grace(timers):
    """The coordinator's mode-to-mode path (``clear_fan=True``, ``OVERRIDE_SUPERSEDED``) through the REAL shared
    helper: the old override is gone, a pending confirmation is cancelled, and the still-running grace is untouched
    (the new override's detection will re-use it)."""
    from custom_components.climate_advisor.override_grace_fsm import OverrideGraceFsmEventKind

    engine = _engine("heat", day="cold", manual_grace_seconds=1800, override_confirm_seconds=0)
    _occupant_overrides(engine, source="normal", new_mode="heat")
    engine._fan_override_active = True
    assert engine._manual_override_active and engine._grace_active

    was_pending = engine._replace_thermostat_override(
        reason="new_override_during_grace", fsm_kind=OverrideGraceFsmEventKind.OVERRIDE_SUPERSEDED, clear_fan=True
    )

    assert was_pending is False
    assert engine._manual_override_active is False and engine._manual_override_mode is None
    assert engine._fan_override_active is False, "mode-to-mode supersession clears the fan override too"
    assert engine._grace_active is True, "OVERRIDE_SUPERSEDED leaves the running grace in place"


# ---------------------------------------------------------------------------
# Issue #1163 / #1164: tell the thermostat fan from the whole-house fan (one predicate)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("grace_s", [0, 1800], ids=["grace0", "grace30m"])
def test_a_pending_change_survives_a_thermostat_fan_press_and_still_confirms(timers, grace_s):
    """The confirmation timer keeps running through a thermostat-fan press (hot day: heat diverges from the plan)."""
    from custom_components.climate_advisor.const import CONF_FAN_MODE

    engine = _engine("heat", day="hot", manual_grace_seconds=grace_s, override_confirm_seconds=600)
    engine.config[CONF_FAN_MODE] = "hvac_fan"
    _occupant_overrides(engine, source="normal", new_mode="heat")
    engine.handle_fan_manual_override(fan_before="auto", fan_after="on", hvac_fan=True)
    assert engine._override_confirm_pending is True

    timers.fire(600)

    assert engine._manual_override_active is True and engine._manual_override_mode == "heat"


def _capture(engine) -> list:
    """Record the qualnames of the tasks the engine schedules and close each coroutine at once (never awaited)."""
    names: list = []

    def _record(coro):
        names.append(coro.__qualname__)
        coro.close()

    engine.hass.async_create_task = MagicMock(side_effect=_record)
    return names


def _suppressed(names) -> bool:
    return any("_suppress_hvac_for_whf" in n for n in names)


class TestOnePredicateGatesTheWhfConsequences:
    """Replacing the occupant's change AND suppressing HVAC are both consequences of a WHOLE-HOUSE-fan press."""

    @pytest.mark.parametrize(
        ("fan_mode", "hvac_fan", "is_whf_press"),
        [
            ("whole_house_fan", False, True),
            ("both", False, True),
            ("whole_house_fan", True, False),
            ("both", True, False),
            ("hvac_fan", False, False),
            ("hvac_fan", True, False),
            ("disabled", False, False),
        ],
    )
    def test_the_predicate(self, timers, fan_mode, hvac_fan, is_whf_press):
        from custom_components.climate_advisor.const import CONF_FAN_MODE

        engine = _engine("heat", day="cold")
        engine.config[CONF_FAN_MODE] = fan_mode
        assert engine._is_whf_press(hvac_fan) is is_whf_press

    @pytest.mark.parametrize("fan_mode", ["whole_house_fan", "both"])
    def test_a_thermostat_blower_press_does_not_suppress_hvac(self, timers, fan_mode):
        from custom_components.climate_advisor.const import CONF_FAN_MODE

        engine = _engine("heat", day="cold", manual_grace_seconds=1800)
        engine.config[CONF_FAN_MODE] = fan_mode
        tasks = _capture(engine)
        engine.handle_fan_manual_override(fan_before="auto", fan_after="on", hvac_fan=True)
        assert _suppressed(tasks) is False, "the blower does not fight the compressor"

    @pytest.mark.parametrize("fan_mode", ["whole_house_fan", "both"])
    @pytest.mark.parametrize("kind", ["entity", "rf"])
    def test_a_whole_house_fan_press_still_suppresses_hvac(self, timers, fan_mode, kind):
        from custom_components.climate_advisor.const import CONF_FAN_MODE

        engine = _engine("heat", day="cold", manual_grace_seconds=1800)
        engine.config[CONF_FAN_MODE] = fan_mode
        tasks = _capture(engine)
        if kind == "entity":
            engine.handle_fan_manual_override(fan_before="off", fan_after="on")
        else:
            engine.handle_fan_manual_override(
                fan_before="off", fan_after="on", duration_override=7200.0, remote_timer_hours=2.0, is_remote_event=True
            )
        assert _suppressed(tasks) is True

    def test_fan_mode_hvac_never_suppresses(self, timers):
        engine = _engine("heat", day="cold", manual_grace_seconds=1800)
        engine.config["fan_mode"] = "hvac_fan"
        tasks = _capture(engine)
        engine.handle_fan_manual_override(fan_before="auto", fan_after="on", hvac_fan=True)
        assert _suppressed(tasks) is False


class TestAFanOffReportOnlyEndsItsOwnFansOverride:
    """#1164: the thermostat blower going On->Auto must not end a WHF override or its RF timer (and vice versa)."""

    def _whf_on_rf_timer(self):
        engine = _engine("heat", day="cold", manual_grace_seconds=1800)
        engine.config["fan_mode"] = "both"
        engine.config[CONF_FAN_ENTITY] = "switch.whf"
        _capture(engine)
        engine.handle_fan_manual_override(
            fan_before="off", fan_after="on", duration_override=7200.0, remote_timer_hours=2.0, is_remote_event=True
        )
        assert engine._fan_override_active and engine._fan_remote_timer_hours == 2.0
        return engine

    def test_blower_off_does_not_end_a_whf_rf_timer_override(self, timers):
        engine = self._whf_on_rf_timer()
        engine.on_fan_turned_off(fan_before="on", fan_after="auto", hvac_fan=True)
        assert engine._fan_override_active is True
        assert engine._fan_remote_timer_hours == 2.0, "the 2 h RF timer is intact"
        assert engine._last_grace_trigger != "fan_off", "no fan-off cooldown replaced the override's grace"

    def test_the_whf_going_off_does_end_the_whf_override(self, timers):
        engine = self._whf_on_rf_timer()
        engine.on_fan_turned_off(fan_before="on", fan_after="off", hvac_fan=False)
        assert engine._fan_override_active is False

    def test_blower_off_still_ends_a_thermostat_fan_override(self, timers):
        engine = _engine("heat", day="cold", manual_grace_seconds=1800)
        engine.config["fan_mode"] = "hvac_fan"
        _capture(engine)
        engine.handle_fan_manual_override(fan_before="auto", fan_after="on", hvac_fan=True)
        assert engine._fan_override_active is True
        engine.on_fan_turned_off(fan_before="on", fan_after="auto", hvac_fan=True)
        assert engine._fan_override_active is False

    def test_symmetry_a_whf_off_report_does_not_end_a_thermostat_fan_override(self, timers):
        engine = _engine("heat", day="cold", manual_grace_seconds=1800)
        engine.config["fan_mode"] = "both"
        engine.config[CONF_FAN_ENTITY] = "switch.whf"
        _capture(engine)
        engine.handle_fan_manual_override(fan_before="auto", fan_after="on", hvac_fan=True)
        engine.on_fan_turned_off(fan_before="on", fan_after="off", hvac_fan=False)
        assert engine._fan_override_active is True


def _mode_change_plus_fan_event(*, new_fan_mode):
    import asyncio

    from tests.test_heat_cool_override import (
        _NOW_BASE,
        _PATCH_CALLBACK,
        _PATCH_DT_UTIL,
        _make_coordinator_stub,
        _make_event,
    )

    coord = _make_coordinator_stub(last_commanded_hvac_mode="heat")
    coord.automation_engine._whf_owns_hvac = MagicMock(return_value=False)
    old_fan = "auto" if new_fan_mode == "on" else "on"
    event = _make_event(
        old_hvac_mode="heat",
        new_hvac_mode="cool",
        old_temp=68.0,
        new_temp=68.0,
        old_fan_mode=old_fan,
        new_fan_mode=new_fan_mode,
    )
    mock_dt = MagicMock()
    mock_dt.now.return_value = _NOW_BASE
    with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
        asyncio.run(coord._async_thermostat_changed(event))
    return coord.automation_engine


def test_a_mode_change_event_still_processes_the_fan_off_direction():
    """Only the fan-ON press is gated by the mode-change override; fan-off/cancel must still end a thermostat-fan
    override (found by code reading; verified here)."""
    ae = _mode_change_plus_fan_event(new_fan_mode="auto")
    assert ae.handle_manual_override.call_count == 1
    ae.on_fan_turned_off.assert_called_once()
    assert ae.on_fan_turned_off.call_args.kwargs.get("hvac_fan") is True


def test_a_mode_change_event_still_skips_the_fan_on_press():
    ae = _mode_change_plus_fan_event(new_fan_mode="on")
    assert ae.handle_manual_override.call_count == 1
    assert ae.handle_fan_manual_override.call_count == 0
