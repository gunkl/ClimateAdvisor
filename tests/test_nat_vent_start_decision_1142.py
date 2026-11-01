"""Issue #1142 D1 (#1140) + D2 (#1138): ONE nat-vent start decision, in the nat-vent FSM.

Owner decisions (2026-11-01):
- D1: with no working fan (fan_mode disabled, or the fan command SUPPRESSED) an open window pauses HVAC
  like any other (household rule 3) instead of starting windows-only nat-vent; the Activity Report must
  not claim a fan transition that never happened.
- D2: the forecast-peak and thermal-floor guards are part of the start decision and hold on every site that
  can start nat-vent (window-open handler, paused-by-door reactivation, idle-open re-entry, grace-expiry
  re-check), re-evaluated every tick. Startup adoption of an already-running fan is excluded.

REAL ``AutomationEngine`` + the state-following fake thermostat (``tests.test_restore_resolved_band``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from custom_components.climate_advisor.automation import AutomationEngine, FanCommandResult
from custom_components.climate_advisor.const import (
    CONF_FAN_MODE,
    FAN_MODE_BOTH,
    FAN_MODE_WHOLE_HOUSE,
)
from custom_components.climate_advisor.nat_vent_fsm import (
    NatVentFsmEvent,
    NatVentFsmEventKind,
    NatVentFsmInputs,
    transition,
)
from custom_components.climate_advisor.nat_vent_gate import (
    FLOOR_IMMINENT,
    FORECAST_PEAK_ABOVE_THRESHOLD,
    NO_WORKING_FAN,
    NatVentStartVetoInputs,
    decide_nat_vent_start_veto,
)
from custom_components.climate_advisor.nat_vent_lifecycle import NatVentLifecycleState
from tests.test_door_pause_recorded_1132 import (
    _AUTOMATION,
    _DAY,
    _close,
    _engine,
    _floor_imminent,
    _forecast_peak_ahead,
    _open,
    _paused_engine,
    _run,
)
from tests.test_restore_resolved_band import _classification, _make_engine

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _no_fan_engine(*, mode: str = "cool", indoor: float = 72.0, outdoor: float = 65.0) -> AutomationEngine:
    """Same mild-day set-up as ``_engine()`` but with NO fan configured (fan_mode defaults to disabled)."""
    engine = _make_engine(indoor=indoor, mode=mode)
    engine._last_outdoor_temp = outdoor
    engine._hourly_forecast_temps = []
    engine._current_classification = _classification("mild")
    engine._sensor_check_callback = lambda: True
    engine._sensor_debounce_pending_callback = lambda: False
    return engine


def _record(engine: AutomationEngine) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    engine._emit_event_callback = lambda name, data: events.append((name, data))
    return events


def _tick(engine: AutomationEngine, at: datetime = _DAY) -> None:
    with (
        patch(f"{_AUTOMATION}.dt_util.now", return_value=at),
        patch(f"{_AUTOMATION}.dt_util.parse_datetime", side_effect=datetime.fromisoformat),
    ):
        _run(engine.check_natural_vent_conditions())


def _re_check(engine: AutomationEngine) -> None:
    with (
        patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY),
        patch(f"{_AUTOMATION}.dt_util.parse_datetime", side_effect=datetime.fromisoformat),
    ):
        _run(engine._re_pause_for_open_sensor())


def _fan_result(engine: AutomationEngine, result: FanCommandResult) -> None:
    """Make the fan command come back as ``result`` (the refusal happens at run time)."""

    async def _fake(**_kwargs):
        return result

    engine._activate_fan = _fake


def _nv_results(events) -> list[str]:
    return [d.get("result") for n, d in events if n == "sensor_opened"]


# ---------------------------------------------------------------------------
# D1 -- no working fan: the window pauses HVAC instead
# ---------------------------------------------------------------------------


class TestNoWorkingFanPausesInstead:
    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_disabled_fan_window_open_pauses_hvac(self, mode):
        engine = _no_fan_engine(mode=mode)
        events = _record(engine)
        _open(engine)
        assert engine._test_state.state == "off", "rule 3: HVAC stays off while the window is open"
        assert engine._paused_by_door is True
        assert engine._pre_pause_mode == mode
        assert engine._natural_vent_active is False
        assert "natural_ventilation" not in _nv_results(events)

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_disabled_fan_pause_is_restored_on_close(self, mode):
        engine = _no_fan_engine(mode=mode)
        _open(engine)
        _close(engine)
        assert engine._paused_by_door is False
        assert engine._test_state.state == mode

    @pytest.mark.parametrize("fan_mode", [FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH])
    def test_with_a_fan_the_same_window_still_starts_nat_vent(self, fan_mode):
        """Mirror / control: only the missing fan changed the answer."""
        engine = _engine(mode="cool")
        engine.config[CONF_FAN_MODE] = fan_mode
        engine.config["fan_entity"] = "fan.whf"
        _open(engine)
        assert engine._natural_vent_active is True
        assert engine._paused_by_door is False

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_suppressed_fan_command_pauses_instead(self, mode):
        """A configured fan that refuses the command (read-back gave up, #835, #893) is no working fan."""
        engine = _engine(mode=mode)
        _fan_result(engine, FanCommandResult.SUPPRESSED)
        events = _record(engine)
        _open(engine)
        assert engine._natural_vent_active is False
        assert engine._paused_by_door is True
        assert engine._test_state.state == "off"
        assert engine._pre_pause_mode == mode
        assert "natural_ventilation" not in _nv_results(events)

    def test_suppressed_fan_command_keeps_pause_at_paused_reactivation(self):
        """Reactivation site (paused-by-door): a refused command must not clear the pause."""
        engine = _paused_engine()
        _fan_result(engine, FanCommandResult.SUPPRESSED)
        events = _record(engine)
        _tick(engine)
        assert engine._paused_by_door is True
        assert engine._natural_vent_active is False
        assert "nat_vent_reactivated_while_paused" not in [n for n, _ in events]

    def test_suppressed_fan_command_idle_open_reports_nothing(self):
        """Idle-open site: no session and no 'natural_ventilation' report for a fan that never started."""
        engine = _engine(mode="off")
        _fan_result(engine, FanCommandResult.SUPPRESSED)
        events = _record(engine)
        _tick(engine)
        assert engine._natural_vent_active is False
        assert "natural_ventilation" not in _nv_results(events)

    def test_disabled_fan_idle_open_starts_nothing(self):
        engine = _no_fan_engine(mode="off")
        events = _record(engine)
        _tick(engine)
        assert engine._natural_vent_active is False
        assert "natural_ventilation" not in _nv_results(events)

    def test_disabled_fan_paused_reactivation_keeps_the_pause(self):
        engine = _paused_engine()
        engine.config.pop(CONF_FAN_MODE)
        _tick(engine)
        assert engine._natural_vent_active is False
        assert engine._paused_by_door is True

    def test_disabled_fan_grace_expiry_recheck_repauses(self):
        engine = _no_fan_engine(mode="cool")
        _re_check(engine)
        assert engine._natural_vent_active is False
        assert engine._paused_by_door is True
        assert engine._test_state.state == "off"


class TestFanClaimOnlyWhenTheFanRan:
    def test_executed_reports_the_fan_transition(self):
        engine = _engine(mode="cool")
        events = _record(engine)
        _open(engine)
        payload = next(d for n, d in events if n == "sensor_opened" and d.get("result") == "natural_ventilation")
        assert payload["fan_mode_change"]

    def test_already_on_does_not_claim_a_transition(self):
        engine = _engine(mode="cool")
        _fan_result(engine, FanCommandResult.ALREADY_IN_STATE)
        events = _record(engine)
        _open(engine)
        payload = next(d for n, d in events if n == "sensor_opened" and d.get("result") == "natural_ventilation")
        assert "fan_mode_change" not in payload


# ---------------------------------------------------------------------------
# D2 -- the guards hold on every start site and are re-evaluated every tick
# ---------------------------------------------------------------------------


def _fan_started(engine) -> bool:
    return engine._natural_vent_active is True


class TestGuardsHoldOnEveryStartSite:
    @pytest.mark.parametrize("guard", [_forecast_peak_ahead, _floor_imminent], ids=["forecast", "floor"])
    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_window_open_handler(self, guard, mode):
        engine = _engine(mode=mode)
        guard(engine)
        _open(engine)
        assert not _fan_started(engine)
        assert engine._paused_by_door is True

    @pytest.mark.parametrize("guard", [_forecast_peak_ahead, _floor_imminent], ids=["forecast", "floor"])
    def test_paused_reactivation_site_waits_then_starts_when_the_guard_clears(self, guard):
        engine = _paused_engine()
        guard(engine)
        events = _record(engine)
        _tick(engine)
        assert not _fan_started(engine), "guard vetoes the paused-by-door reactivation"
        assert engine._paused_by_door is True
        assert "nat_vent_reactivated_while_paused" not in [n for n, _ in events]
        # re-evaluated every tick: the forecast peak ages out / the estimate moves -> next tick starts
        engine._hourly_forecast_temps = []
        engine._thermal_model = {}
        _tick(engine)
        assert _fan_started(engine)
        assert engine._paused_by_door is False

    @pytest.mark.parametrize("guard", [_forecast_peak_ahead, _floor_imminent], ids=["forecast", "floor"])
    def test_idle_open_site_waits_then_starts_when_the_guard_clears(self, guard):
        engine = _engine(mode="off")
        guard(engine)
        _tick(engine)
        assert not _fan_started(engine)
        engine._hourly_forecast_temps = []
        engine._thermal_model = {}
        _tick(engine)
        assert _fan_started(engine)

    @pytest.mark.parametrize("guard", [_forecast_peak_ahead, _floor_imminent], ids=["forecast", "floor"])
    def test_grace_expiry_recheck_repauses_instead_of_starting(self, guard):
        engine = _paused_engine()
        guard(engine)
        _re_check(engine)
        assert not _fan_started(engine)
        assert engine._paused_by_door is True
        assert engine._test_state.state == "off"

    def test_grace_expiry_recheck_starts_when_no_guard_applies(self):
        """Control for the previous test: same engine, no guard -> nat-vent takes the window."""
        engine = _paused_engine()
        _re_check(engine)
        assert _fan_started(engine)
        assert engine._paused_by_door is False


class TestGuardsAreOnlyOnStarts:
    def test_running_session_is_not_ended_by_a_forecast_peak(self):
        """The guards are start rules, not exit rules: a session already running keeps running."""
        engine = _engine(mode="off")
        _tick(engine)
        assert _fan_started(engine)
        _forecast_peak_ahead(engine)
        _tick(engine, _DAY + timedelta(minutes=1))
        assert _fan_started(engine)

    def test_startup_adoption_inputs_carry_no_guards(self):
        """Startup adoption (``reconcile_fan_on_startup``) builds its FSM inputs without start guards."""
        engine = _engine(mode="off")
        _forecast_peak_ahead(engine)
        with patch(f"{_AUTOMATION}.dt_util.parse_datetime", side_effect=datetime.fromisoformat):
            default = engine._build_nat_vent_fsm_inputs(now=_DAY, indoor=72.0, outdoor=65.0)
            guarded = engine._build_nat_vent_fsm_inputs(now=_DAY, indoor=72.0, outdoor=65.0, start_guards=True)
        assert default.start_veto is None
        assert guarded.start_veto is not None
        assert guarded.start_veto.forecast_peak_2h == 95.0


# ---------------------------------------------------------------------------
# pure pieces
# ---------------------------------------------------------------------------


def _veto_inputs(**kw) -> NatVentStartVetoInputs:
    base = {
        "outdoor": 65.0,
        "indoor": 72.0,
        "comfort_heat": 68.0,
        "comfort_cool": 74.0,
        "nat_vent_delta": 3.0,
        "forecast_peak_2h": None,
        "thermal_confidence": "none",
        "thermal_k_passive": None,
    }
    base.update(kw)
    return NatVentStartVetoInputs(**base)


class TestPureVeto:
    def test_forecast_peak_above_threshold(self):
        v = decide_nat_vent_start_veto(_veto_inputs(forecast_peak_2h=78.0))
        assert v is not None and v.reason == FORECAST_PEAK_ABOVE_THRESHOLD and v.threshold == 77.0

    def test_forecast_peak_at_threshold_is_not_a_veto(self):
        assert decide_nat_vent_start_veto(_veto_inputs(forecast_peak_2h=77.0)) is None

    def test_floor_imminent_needs_a_confident_model(self):
        hot = {"thermal_k_passive": -1.0, "outdoor": 40.0}
        assert decide_nat_vent_start_veto(_veto_inputs(thermal_confidence="low", **hot)) is None
        v = decide_nat_vent_start_veto(_veto_inputs(thermal_confidence="high", **hot))
        assert v is not None and v.reason == FLOOR_IMMINENT

    def test_forecast_guard_wins_when_both_hold(self):
        v = decide_nat_vent_start_veto(
            _veto_inputs(forecast_peak_2h=90.0, thermal_confidence="high", thermal_k_passive=-1.0, outdoor=40.0)
        )
        assert v.reason == FORECAST_PEAK_ABOVE_THRESHOLD


def _fsm_inputs(**kw) -> NatVentFsmInputs:
    base = {
        "indoor": 72.0,
        "outdoor": 65.0,
        "comfort_heat_raw": 68.0,
        "sleep_heat": 66.0,
        "in_sleep_window": False,
        "comfort_cool": 74.0,
        "nat_vent_delta": 3.0,
        "hysteresis": 0.0,
        "fan_mode": FAN_MODE_WHOLE_HOUSE,
        "aggressive_savings": False,
        "occupancy_mode": "home",
        "outdoor_today_peak": None,
        "outdoor_sample_count": 0,
        "peak_decline_margin": 3.0,
        "paused_by_door": False,
        "outdoor_exit_time": None,
        "lockout_seconds": 300.0,
        "now": _DAY,
    }
    base.update(kw)
    return NatVentFsmInputs(**base)


def _start(**kw):
    return transition(NatVentLifecycleState.INACTIVE, NatVentFsmEvent(NatVentFsmEventKind.TICK, _fsm_inputs(**kw)))


class TestFsmStartDecision:
    def test_start_with_a_fan(self):
        t = _start()
        assert t.to_state == NatVentLifecycleState.ACTIVE_FULL_GATE and t.veto is None

    def test_no_fan_is_a_veto_not_a_failed_gate(self):
        t = _start(fan_mode="disabled")
        assert t.to_state == NatVentLifecycleState.INACTIVE
        assert t.veto is not None and t.veto.reason == NO_WORKING_FAN
        assert t.vetoed_state == NatVentLifecycleState.ACTIVE_FULL_GATE

    def test_conditions_not_met_is_not_a_veto(self):
        t = _start(outdoor=90.0)
        assert t.to_state == NatVentLifecycleState.INACTIVE and t.veto is None

    def test_guards_only_when_the_caller_supplies_them(self):
        guard = _veto_inputs(forecast_peak_2h=95.0)
        assert _start().veto is None
        assert _start(start_veto=guard).veto.reason == FORECAST_PEAK_ABOVE_THRESHOLD
