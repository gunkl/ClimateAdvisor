"""Issue #1143: a door/window pause that CA itself starts from a running cycle is recorded as ACTIVE.

Occupant framing: the window opens while heat or cool is running and Climate Advisor turns the
system off for it. The pause was recorded as if HVAC had ALREADY been off (PAUSED_IDLE), so when
conditions later favour ventilation the "reactivate nat-vent while paused" branch of
``check_natural_vent_conditions()`` was skipped, the idle-open re-entry started the fan instead, and
the pause flags and ``_paused_entity`` were left set while the fan ran.

Root cause (regression from d8e235a, #757 Phase 6 Step 4): ``_build_door_window_fsm_inputs()`` reads
the live thermostat mode AFTER ``_pause_for_door_window_action()`` has commanded "off" (the climate
entity updates inside the blocking ``set_hvac_mode`` call), and ``decide_pause_entry`` maps "off" to
PAUSE_IDLE. The action already returns the authoritative ``hvac_already_off``.

These tests use the REAL ``AutomationEngine`` with the state-following fake thermostat from
``tests.test_restore_resolved_band`` (``set_hvac_mode`` really changes the state it reports); a
static ``MagicMock`` state hides this defect.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from custom_components.climate_advisor.door_window_fsm import (
    DoorWindowFsmEvent,
    DoorWindowFsmEventKind,
    DoorWindowFsmInputs,
    transition,
)
from custom_components.climate_advisor.door_window_lifecycle import DoorWindowLifecycleState
from tests.test_door_pause_recorded_1132 import (
    _AUTOMATION,
    _DAY,
    _engine,
    _forecast_peak_ahead,
    _open,
    _run,
)


def _fsm_inputs(**overrides) -> DoorWindowFsmInputs:
    base = {
        "hvac_mode": "off",
        "outdoor": 95.0,
        "indoor": 72.0,
        "comfort_heat": 68.0,
        "comfort_cool": 74.0,
        "nat_vent_delta": 3.0,
        "fan_mode": "whole_house_fan",
        "aggressive_savings": False,
        "within_planned_window": False,
        "any_sensor_open": True,
        "sensor_debounce_pending": False,
        "override_matches_current_decision": False,
        "grace_source": "automation",
        "natural_vent_active": False,
        "whf_owns_hvac": False,
        "grace_active": False,
        "pre_pause_mode_active": False,
        "now": datetime(2026, 10, 28, 14, 0, tzinfo=UTC),
        "nat_vent_gate_ruled_out": True,
    }
    base.update(overrides)
    return DoorWindowFsmInputs(**base)


def _sensor_opened(origin: DoorWindowLifecycleState, **overrides) -> DoorWindowLifecycleState:
    event = DoorWindowFsmEvent(DoorWindowFsmEventKind.SENSOR_OPENED, _fsm_inputs(**overrides))
    return transition(origin, event).to_state


# ---------------------------------------------------------------------------
# The FSM maps the action's authoritative answer, not the (already-off) live mode
# ---------------------------------------------------------------------------


class TestFsmUsesAuthoritativeAlreadyOff:
    def test_active_pause_from_normal_with_live_mode_already_off(self):
        """live mode says off (CA just wrote it) but the action says it was running -> PAUSED_ACTIVE."""
        assert (
            _sensor_opened(DoorWindowLifecycleState.NORMAL, hvac_mode="off", hvac_already_off=False)
            == DoorWindowLifecycleState.PAUSED_ACTIVE
        )

    def test_idle_pause_from_normal(self):
        """Mirror: HVAC really was off -> PAUSED_IDLE."""
        assert (
            _sensor_opened(DoorWindowLifecycleState.NORMAL, hvac_mode="off", hvac_already_off=True)
            == DoorWindowLifecycleState.PAUSED_IDLE
        )

    @pytest.mark.parametrize("already_off", [True, False])
    def test_grace_origin_stays_paused_during_grace(self, already_off):
        """GRACE origin is unaffected by the sub-state (composite state), either answer."""
        assert (
            _sensor_opened(DoorWindowLifecycleState.GRACE, grace_active=True, hvac_already_off=already_off)
            == DoorWindowLifecycleState.PAUSED_DURING_GRACE
        )

    @pytest.mark.parametrize(
        ("pre_pause_mode_active", "expected"),
        [(True, DoorWindowLifecycleState.PAUSED_ACTIVE), (False, DoorWindowLifecycleState.PAUSED_IDLE)],
    )
    def test_grace_expiry_while_paused_uses_pre_pause_mode_not_live_mode(self, pre_pause_mode_active, expected):
        """By expiry CA has turned HVAC off, so the live mode reads "off" either way; the restore target decides."""
        event = DoorWindowFsmEvent(
            DoorWindowFsmEventKind.GRACE_TIMER_EXPIRED,
            _fsm_inputs(hvac_mode="off", pre_pause_mode_active=pre_pause_mode_active),
        )
        assert transition(DoorWindowLifecycleState.PAUSED_DURING_GRACE, event).to_state == expected

    def test_no_authoritative_answer_falls_back_to_live_mode(self):
        """Callers without a pre-command reading (hvac_already_off=None) keep the live-mode split."""
        assert (
            _sensor_opened(DoorWindowLifecycleState.NORMAL, hvac_mode="cool", hvac_already_off=None)
            == DoorWindowLifecycleState.PAUSED_ACTIVE
        )
        assert (
            _sensor_opened(DoorWindowLifecycleState.NORMAL, hvac_mode="off", hvac_already_off=None)
            == DoorWindowLifecycleState.PAUSED_IDLE
        )


# ---------------------------------------------------------------------------
# End to end through the real engine
# ---------------------------------------------------------------------------


class TestActivePauseRecordedAsActive:
    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_handle_door_window_open_fall_through_pause(self, mode):
        """A forecast-peak skip pauses a RUNNING cycle: recorded as an active pause (heat and cool)."""
        engine = _engine(mode=mode)
        _forecast_peak_ahead(engine)
        _open(engine)
        assert engine._test_state.state == "off"
        assert engine._paused_by_door is True
        assert engine._pre_pause_mode == mode
        assert engine._paused_with_hvac_already_off is False
        assert engine.door_window_lifecycle_state == DoorWindowLifecycleState.PAUSED_ACTIVE

    def test_pause_when_hvac_already_off_is_idle(self):
        """Mirror: the occupant had HVAC off -> still the idle pause."""
        engine = _engine(mode="off")
        _forecast_peak_ahead(engine)
        _open(engine)
        assert engine._paused_by_door is True
        assert engine._paused_with_hvac_already_off is True
        assert engine.door_window_lifecycle_state == DoorWindowLifecycleState.PAUSED_IDLE

    def test_pause_for_door_window_wrapper_active(self):
        """The generic wrapper (comfort-band guard, sync re-check, grace re-pause) records the same way."""
        engine = _engine(mode="cool")
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
            _run(
                engine._pause_for_door_window(
                    entity_label="monitored door/window", reason="test", notify_message="paused", notify_type="x"
                )
            )
        assert engine._paused_by_door is True
        assert engine._paused_with_hvac_already_off is False
        assert engine.door_window_lifecycle_state == DoorWindowLifecycleState.PAUSED_ACTIVE

    def test_pause_for_door_window_wrapper_idle(self):
        engine = _engine(mode="off")
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
            _run(
                engine._pause_for_door_window(
                    entity_label="monitored door/window", reason="test", notify_message="paused", notify_type="x"
                )
            )
        assert engine._paused_by_door is True
        assert engine._paused_with_hvac_already_off is True
        assert engine.door_window_lifecycle_state == DoorWindowLifecycleState.PAUSED_IDLE

    def test_grace_origin_pause_is_paused_during_grace(self):
        engine = _engine(mode="cool")
        _forecast_peak_ahead(engine)
        engine._grace_active = True
        engine._last_grace_trigger = "automation"
        engine._last_resume_source = "automation"
        _open(engine)
        assert engine._paused_by_door is True
        assert engine.door_window_lifecycle_state == DoorWindowLifecycleState.PAUSED_DURING_GRACE


class TestReactivationAfterActivePauseTakesPausedBranch:
    def _reactivate(self, engine) -> list[str]:
        events: list[str] = []
        engine._emit_event_callback = lambda name, data: events.append(name)
        engine._hourly_forecast_temps = []  # the forecast peak that caused the pause has passed
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
            _run(engine.check_natural_vent_conditions())
        return events

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_active_pause_then_conditions_clear(self, mode):
        engine = _engine(mode=mode)
        _forecast_peak_ahead(engine)
        _open(engine)
        assert engine._paused_by_door is True
        events = self._reactivate(engine)
        assert "nat_vent_reactivated_while_paused" in events, "the paused-by-door reactivation branch must run"
        assert "sensor_opened" not in events, "the idle-open re-entry must not have handled it"
        assert engine._natural_vent_active is True
        assert engine._paused_by_door is False
        assert engine._paused_entity is None
        assert engine._pre_pause_mode is None

    def test_idle_pause_then_conditions_clear_uses_idle_open_path(self):
        """Mirror: HVAC was already off, so the idle-open re-entry (not the paused branch) starts the fan."""
        engine = _engine(mode="off")
        _forecast_peak_ahead(engine)
        _open(engine)
        assert engine._paused_with_hvac_already_off is True
        events = self._reactivate(engine)
        assert "nat_vent_reactivated_while_paused" not in events
        assert engine._natural_vent_active is True


class TestGraceExpiryKeepsActivePauseSubstate:
    """Grace expires while the window is still open after CA paused a running cycle (PAUSED_DURING_GRACE)."""

    def _paused_during_grace(self, mode):
        engine = _engine(mode=mode)
        _forecast_peak_ahead(engine)
        engine._grace_active = True
        engine._last_grace_trigger = "automation"
        engine._last_resume_source = "automation"
        _open(engine)
        assert engine.door_window_lifecycle_state == DoorWindowLifecycleState.PAUSED_DURING_GRACE
        assert engine._paused_with_hvac_already_off is False
        return engine

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_expiry_stays_active_and_next_check_takes_paused_branch(self, mode):
        engine = self._paused_during_grace(mode)
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
            engine._on_grace_expired("automation", 300, False)
        assert engine._grace_active is False
        assert engine._paused_by_door is True
        assert engine._test_state.state == "off"  # the live mode the old derivation misread
        assert engine._paused_with_hvac_already_off is False
        assert engine.door_window_lifecycle_state == DoorWindowLifecycleState.PAUSED_ACTIVE

        events = TestReactivationAfterActivePauseTakesPausedBranch()._reactivate(engine)
        assert "nat_vent_reactivated_while_paused" in events
        assert "sensor_opened" not in events
        assert engine._paused_by_door is False
        assert engine._paused_entity is None
        assert engine._pre_pause_mode is None

    def test_expiry_stays_idle_when_hvac_was_already_off(self):
        engine = _engine(mode="off")
        _forecast_peak_ahead(engine)
        engine._grace_active = True
        engine._last_grace_trigger = "automation"
        engine._last_resume_source = "automation"
        _open(engine)
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
            engine._on_grace_expired("automation", 300, False)
        assert engine._paused_by_door is True
        assert engine._paused_with_hvac_already_off is True
        assert engine.door_window_lifecycle_state == DoorWindowLifecycleState.PAUSED_IDLE
