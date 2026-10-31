"""Issue #1132: a door/window pause must always be RECORDED when HVAC is turned off for it.

Occupant framing: a window opens while the occupant's manual thermostat change is in effect (or while
a forecast/thermal guard has just ruled out natural ventilation). Climate Advisor turns the HVAC off
for the open window but never records the pause. When the window closes nothing is restored and the
house stays without heating/cooling until the occupant touches the thermostat.

Three defects, all covered with the REAL ``AutomationEngine`` + the fake thermostat from
``tests.test_restore_resolved_band`` (nothing re-implements production logic):

1. ``handle_door_window_open()`` dispatched the SENSOR_OPENED FSM event WITHOUT
   ``nat_vent_gate_ruled_out=True``. The legacy decision had already chosen PAUSE (and turned HVAC off),
   but the FSM's simplified gate cannot see the forecast-peak/thermal-floor guards or a manual override
   and answered "nat-vent eligible" / "grace suppressed" -> no pause recorded. Both FSM origins
   (NORMAL and GRACE) are covered.
2. Mirror of #931: the paused-by-door nat-vent reactivation sites cleared the pause without checking
   whether ``_activate_fan()`` actually took effect (overridden / rate-limited fan) -> HVAC off, no pause,
   no nat-vent session.
3. ``_pre_pause_mode`` could survive with no pause recorded, so a LATER window opened while HVAC was
   already off closed by restoring a mode the occupant had off.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.const import CONF_FAN_MODE, FAN_MODE_WHOLE_HOUSE
from tests.test_restore_resolved_band import _classification, _make_engine, _run

_AUTOMATION = "custom_components.climate_advisor.automation"
_DAY = datetime(2026, 10, 28, 14, 0, 0, tzinfo=UTC)
_SENSOR = "binary_sensor.front_window"


def _engine(
    *, mode: str = "cool", indoor: float = 72.0, outdoor: float = 65.0, sensor_open: bool = True
) -> AutomationEngine:
    """Engine on a mild day where the plain nat-vent entry gate is satisfied (outdoor < indoor)."""
    engine = _make_engine(indoor=indoor, mode=mode, **{CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE})
    engine._last_outdoor_temp = outdoor
    engine._hourly_forecast_temps = []
    engine._current_classification = _classification("mild")
    engine._sensor_check_callback = lambda: sensor_open
    engine._sensor_debounce_pending_callback = lambda: False
    return engine


def _forecast_peak_ahead(engine: AutomationEngine) -> None:
    """Phase 2 Guard 1: a 95F forecast peak within 2 h skips nat-vent."""
    engine._hourly_forecast_temps = [
        {"temperature": 95.0, "datetime": (_DAY + timedelta(hours=1)).isoformat()},
    ]


def _floor_imminent(engine: AutomationEngine) -> None:
    """Phase 2 Guard 2: the thermal model predicts the comfort floor within MIN_VIABLE_NAT_VENT_HOURS."""
    engine._thermal_model = {"confidence": "high", "k_passive": -1.0}


def _open(engine: AutomationEngine) -> None:
    # dt_util is a stub in the lightweight HA environment: parse_datetime must really parse ISO strings.
    with (
        patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY),
        patch(f"{_AUTOMATION}.dt_util.parse_datetime", side_effect=datetime.fromisoformat),
    ):
        _run(engine.handle_door_window_open(_SENSOR))


def _close(engine: AutomationEngine) -> None:
    engine._sensor_check_callback = lambda: False
    with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY + timedelta(minutes=30)):
        _run(engine.handle_all_doors_windows_closed())


def _assert_pause_recorded_and_restored(engine: AutomationEngine, *, was: str = "cool") -> None:
    assert engine._test_state.state == "off", "precondition: HVAC was turned off for the open window"
    assert engine._paused_by_door is True, "HVAC was turned off for the window, so the pause must be recorded"
    assert engine._pre_pause_mode == was
    _close(engine)
    assert engine._paused_by_door is False
    assert engine._test_state.state == was, "closing the window must restore what the occupant had"


# ---------------------------------------------------------------------------
# 1. Missing nat_vent_gate_ruled_out at handle_door_window_open()
# ---------------------------------------------------------------------------


class TestPauseRecordedWhenNatVentSkipped:
    def test_forecast_peak_guard_skip_records_pause_and_close_restores(self):
        engine = _engine()
        _forecast_peak_ahead(engine)
        _open(engine)
        _assert_pause_recorded_and_restored(engine)

    def test_thermal_floor_guard_skip_records_pause_and_close_restores(self):
        engine = _engine()
        _floor_imminent(engine)
        _open(engine)
        _assert_pause_recorded_and_restored(engine)

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_manual_override_without_grace_is_not_paused_at_all(self, mode):
        """Manual Change Pause = 0: an override is active, no grace is running. #1132 recorded the pause
        for this shape; Issue #1133 (owner decision, 2026-10-30) went further -- a window opening later
        must not pause HVAC over a confirmed override at all, so there is no pause to record and the
        occupant's mode is left exactly as they set it."""
        engine = _engine(mode=mode)
        engine._manual_override_active = True
        engine._manual_override_mode = mode
        engine._manual_override_source = "pause"
        engine._grace_active = False
        _open(engine)
        assert engine._paused_by_door is False
        assert engine._test_state.state == mode
        assert engine._pre_pause_mode is None

    def test_grace_origin_skip_records_pause(self):
        """GRACE origin: a live grace whose reactivation gate is viable falls through the grace
        exemption, then a Phase 2 guard skips nat-vent -> pause must still be recorded."""
        engine = _engine()
        _forecast_peak_ahead(engine)
        engine._grace_active = True
        engine._last_grace_trigger = "automation"
        engine._last_resume_source = "automation"
        _open(engine)
        assert engine._test_state.state == "off"
        assert engine._paused_by_door is True
        assert engine._pre_pause_mode == "cool"

    def test_no_stale_pre_pause_mode_when_pause_not_recordable(self):
        """Thermostat state unavailable: no pause happens at all and no restore target is left behind."""
        engine = _engine(mode="unavailable")
        engine._pre_pause_mode = "heat"
        _forecast_peak_ahead(engine)
        _open(engine)
        assert engine._paused_by_door is False
        # nothing was recorded, so nothing may be left to restore
        engine._sensor_check_callback = lambda: False
        _close(engine)
        assert engine._pre_pause_mode is None


# ---------------------------------------------------------------------------
# 2. Mirror of #931: reactivation clears the pause only when the fan command took effect
# ---------------------------------------------------------------------------


def _paused_engine(*, entity: str = _SENSOR) -> AutomationEngine:
    """Actively paused for an open window; nat-vent conditions (outdoor 65 < indoor 72) are met."""
    engine = _engine(mode="off")
    engine._paused_by_door = True
    engine._paused_with_hvac_already_off = False
    engine._pre_pause_mode = "cool"
    engine._paused_entity = entity
    engine._paused_since = _DAY - timedelta(minutes=20)
    return engine


def _overridden(engine: AutomationEngine) -> None:
    """The occupant's fan override lands DURING the activation await (the Issue #706 Bug F race), so the
    nat-vent gate passes and the real ``_activate_fan()`` then answers OVERRIDDEN."""
    real_activate = engine._activate_fan

    async def _override_arrives_mid_await(**kwargs):
        engine._fan_override_active = True
        return await real_activate(**kwargs)

    engine._activate_fan = _override_arrives_mid_await


def _rate_limited(engine: AutomationEngine) -> None:
    engine._fan_toggle_command_time = _DAY - timedelta(seconds=10)


def _reactivate_via_periodic_check(engine: AutomationEngine) -> None:
    with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
        _run(engine.check_natural_vent_conditions())


def _reactivate_via_grace_expiry(engine: AutomationEngine) -> None:
    with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
        _run(engine._re_pause_for_open_sensor())


_SITES = {
    "check_natural_vent_conditions": _reactivate_via_periodic_check,
    "re_pause_for_open_sensor": _reactivate_via_grace_expiry,
}


class TestReactivationClearsPauseOnlyOnFanSuccess:
    @pytest.mark.parametrize("site", list(_SITES))
    @pytest.mark.parametrize("block", [_overridden, _rate_limited], ids=["overridden", "rate_limited"])
    def test_refused_fan_command_keeps_pause_and_restore_target(self, site, block):
        engine = _paused_engine()
        block(engine)
        events: list[str] = []
        engine._emit_event_callback = lambda name, data: events.append(name)
        pause_flips: list[tuple[bool, bool]] = []
        real_emit = engine._emit_boolean_transition

        def _record(**kw):
            if kw.get("caller") in ("_apply_nat_vent_fsm_state", "_keep_pause_after_refused_reactivation"):
                pause_flips.append((kw["before"], kw["after"]))
            return real_emit(**kw)

        engine._emit_boolean_transition = _record

        _SITES[site](engine)

        assert engine._paused_by_door is True, "fan was refused -> no nat-vent session -> the pause must stay"
        assert [f for f in pause_flips if f[0] != f[1]] == [], "a pause that never ended must not log ENDED/STARTED"
        assert engine._natural_vent_active is False
        assert engine._pre_pause_mode == "cool"
        assert engine._paused_entity is not None
        assert "nat_vent_reactivated_while_paused" not in events
        # and the window closing still restores what the occupant had
        _close(engine)
        assert engine._test_state.state == "cool"

    @pytest.mark.parametrize("site", list(_SITES))
    def test_executed_fan_clears_pause_and_records_session(self, site):
        """Positive control: the same setup with a working fan hands the window to nat-vent."""
        engine = _paused_engine()
        events: list[str] = []
        engine._emit_event_callback = lambda name, data: events.append(name)

        _SITES[site](engine)

        assert engine._natural_vent_active is True
        assert engine._paused_by_door is False
        assert engine._paused_entity is None
        assert "nat_vent_reactivated_while_paused" in events


# ---------------------------------------------------------------------------
# 3. Stale _pre_pause_mode must not restore a mode the occupant had off
# ---------------------------------------------------------------------------


class TestStalePrePauseModeNeverRestored:
    def test_hvac_already_off_window_ignores_stale_value(self):
        """HVAC was off when this window opened -> nothing to restore, whatever an earlier pause left."""
        engine = _engine(mode="off", outdoor=90.0)  # too warm for nat-vent -> plain pause
        engine._pre_pause_mode = "cool"  # stale leftover from an earlier unrecorded/unrestored pause
        _open(engine)
        assert engine._paused_by_door is True
        assert engine._pre_pause_mode is None
        _close(engine)
        assert engine._test_state.state == "off", "the occupant had HVAC off; closing the window must not turn it on"

    def test_close_without_recorded_pause_clears_stale_value(self):
        engine = _engine(mode="off", sensor_open=False)
        engine._paused_by_door = False
        engine._pre_pause_mode = "cool"
        _close(engine)
        assert engine._pre_pause_mode is None

    @pytest.mark.parametrize("site", list(_SITES))
    def test_reactivation_that_ends_the_pause_does_not_leave_restore_target(self, site):
        engine = _paused_engine()
        _SITES[site](engine)
        assert engine._paused_by_door is False
        assert engine._pre_pause_mode is None

    def test_already_paused_repause_keeps_restore_target(self):
        """_re_pause_for_open_sensor() on an already-paused engine (HVAC off because WE paused it)
        must keep the restore target -- the off-branch reset applies only when no pause is recorded."""
        engine = _paused_engine()
        engine._last_outdoor_temp = 95.0  # reactivation not viable -> plain re-pause
        _reactivate_via_grace_expiry(engine)
        assert engine._paused_by_door is True
        assert engine._pre_pause_mode == "cool"
