"""Wiring tests for Issue #637 (Block 5 Phase 2): door/window pause FSM assembly.

Each of the 5 pure pieces this FSM assembles already has its own exhaustive
unit coverage (``tests/test_door_window_pure_modules.py``) — these tests focus
on wiring correctness: does ``transition()`` route to the right pure function
given the right adapted inputs, and land on the state the corresponding real
``automation.py`` code path actually reaches. Mirrors
``tests/test_nat_vent_fsm.py``'s structure.
"""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.climate_advisor.door_window_fsm import (
    DoorWindowFsmEvent,
    DoorWindowFsmEventKind,
    DoorWindowFsmInputs,
    transition,
)
from custom_components.climate_advisor.door_window_lifecycle import DoorWindowLifecycleState

_NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)


def _inputs(**overrides) -> DoorWindowFsmInputs:
    base = dict(
        hvac_mode="cool",
        outdoor=85.0,
        indoor=76.0,
        comfort_heat=68.0,
        comfort_cool=78.0,
        nat_vent_delta=2.0,
        fan_mode="disabled",
        aggressive_savings=False,
        within_planned_window=False,
        any_sensor_open=True,
        sensor_debounce_pending=False,
        override_matches_current_decision=False,
        grace_source="manual",
        natural_vent_active=False,
        whf_owns_hvac=False,
        grace_active=False,
        pre_pause_mode_active=False,
        now=_NOW,
    )
    base.update(overrides)
    return DoorWindowFsmInputs(**base)


def _ev(kind: DoorWindowFsmEventKind, **overrides) -> DoorWindowFsmEvent:
    return DoorWindowFsmEvent(kind=kind, inputs=_inputs(**overrides))


class TestFromNormal:
    def test_sensor_opened_active_pause(self):
        # outdoor 85 > threshold 80 -> nat-vent gate fails -> pause; hvac_mode "cool" -> active
        t = transition(DoorWindowLifecycleState.NORMAL, _ev(DoorWindowFsmEventKind.SENSOR_OPENED))
        assert t.to_state == DoorWindowLifecycleState.PAUSED_ACTIVE
        assert t.changed

    def test_sensor_opened_idle_pause(self):
        t = transition(DoorWindowLifecycleState.NORMAL, _ev(DoorWindowFsmEventKind.SENSOR_OPENED, hvac_mode="off"))
        assert t.to_state == DoorWindowLifecycleState.PAUSED_IDLE

    def test_sensor_opened_nat_vent_eligible_stays_normal(self):
        # outdoor 60 < indoor 76, outdoor < threshold 80 -> nat-vent gate passes
        t = transition(DoorWindowLifecycleState.NORMAL, _ev(DoorWindowFsmEventKind.SENSOR_OPENED, outdoor=60.0))
        assert t.to_state == DoorWindowLifecycleState.NORMAL
        assert t.outcome == "nat_vent_eligible"

    def test_sensor_opened_planned_window_noop(self):
        t = transition(
            DoorWindowLifecycleState.NORMAL,
            _ev(DoorWindowFsmEventKind.SENSOR_OPENED, within_planned_window=True),
        )
        assert t.to_state == DoorWindowLifecycleState.NORMAL
        assert t.outcome == "planned_window_noop"

    def test_all_sensors_closed_noop(self):
        t = transition(DoorWindowLifecycleState.NORMAL, _ev(DoorWindowFsmEventKind.ALL_SENSORS_CLOSED))
        assert t.to_state == DoorWindowLifecycleState.NORMAL

    def test_dashboard_resume_unreachable_noop(self):
        t = transition(DoorWindowLifecycleState.NORMAL, _ev(DoorWindowFsmEventKind.DASHBOARD_RESUME))
        assert t.to_state == DoorWindowLifecycleState.NORMAL

    def test_sync_reconcile_pauses_when_sensor_open_and_untracked(self):
        t = transition(
            DoorWindowLifecycleState.NORMAL,
            _ev(DoorWindowFsmEventKind.SYNC_RECONCILE, any_sensor_open=True, hvac_mode="heat"),
        )
        assert t.to_state == DoorWindowLifecycleState.PAUSED_ACTIVE

    def test_sync_reconcile_noop_when_no_sensor_open(self):
        t = transition(
            DoorWindowLifecycleState.NORMAL,
            _ev(DoorWindowFsmEventKind.SYNC_RECONCILE, any_sensor_open=False),
        )
        assert t.to_state == DoorWindowLifecycleState.NORMAL

    def test_sync_reconcile_noop_when_nat_vent_active(self):
        t = transition(
            DoorWindowLifecycleState.NORMAL,
            _ev(DoorWindowFsmEventKind.SYNC_RECONCILE, any_sensor_open=True, natural_vent_active=True),
        )
        assert t.to_state == DoorWindowLifecycleState.NORMAL

    def test_nat_vent_exited_sensor_still_open_active(self):
        t = transition(
            DoorWindowLifecycleState.NORMAL,
            _ev(DoorWindowFsmEventKind.NAT_VENT_EXITED_SENSOR_STILL_OPEN, hvac_mode="cool"),
        )
        assert t.to_state == DoorWindowLifecycleState.PAUSED_ACTIVE

    def test_nat_vent_exited_sensor_still_open_idle_when_mode_off(self):
        # _exit_nat_vent()'s pre_pause_mode read is `state.state if state and
        # state.state != "off" else None` — "unavailable" is a non-None string
        # (production would literally store pre_pause_mode="unavailable"),
        # which this module treats as active per the same != "off" test. Only
        # a literal "off" mode collapses to idle.
        t = transition(
            DoorWindowLifecycleState.NORMAL,
            _ev(DoorWindowFsmEventKind.NAT_VENT_EXITED_SENSOR_STILL_OPEN, hvac_mode="off"),
        )
        assert t.to_state == DoorWindowLifecycleState.PAUSED_IDLE


class TestFromPaused:
    def test_all_sensors_closed_active_restores_and_graces(self):
        t = transition(
            DoorWindowLifecycleState.PAUSED_ACTIVE,
            _ev(DoorWindowFsmEventKind.ALL_SENSORS_CLOSED, pre_pause_mode_active=True),
        )
        assert t.to_state == DoorWindowLifecycleState.GRACE

    def test_all_sensors_closed_idle_clears_no_grace(self):
        t = transition(
            DoorWindowLifecycleState.PAUSED_IDLE,
            _ev(DoorWindowFsmEventKind.ALL_SENSORS_CLOSED, pre_pause_mode_active=False),
        )
        assert t.to_state == DoorWindowLifecycleState.NORMAL

    def test_all_sensors_closed_idle_origin_but_pre_pause_mode_active_restores(self):
        """Issue #660 Step 2: the ALL_SENSORS_CLOSED branch used to infer
        pre_pause_mode from `current_state == PAUSED_ACTIVE`, a placeholder rather
        than the real AutomationEngine._pre_pause_mode value. PAUSED_ACTIVE vs.
        PAUSED_IDLE depends on _paused_with_hvac_already_off, which a still-legacy
        call site (_exit_nat_vent()'s sensor-still-open branch) could leave stale —
        so a reachable case exists where current_state == PAUSED_IDLE but the real
        pre_pause_mode is still set (truthy). This must now correctly restore/grace,
        not silently clear with no restore because the origin-state proxy said IDLE."""
        t = transition(
            DoorWindowLifecycleState.PAUSED_IDLE,
            _ev(DoorWindowFsmEventKind.ALL_SENSORS_CLOSED, pre_pause_mode_active=True),
        )
        assert t.to_state == DoorWindowLifecycleState.GRACE

    def test_manual_override_during_pause_clears_to_normal(self):
        t = transition(DoorWindowLifecycleState.PAUSED_ACTIVE, _ev(DoorWindowFsmEventKind.MANUAL_OVERRIDE_DURING_PAUSE))
        assert t.to_state == DoorWindowLifecycleState.NORMAL

    def test_dashboard_resume_starts_grace(self):
        t = transition(DoorWindowLifecycleState.PAUSED_IDLE, _ev(DoorWindowFsmEventKind.DASHBOARD_RESUME))
        assert t.to_state == DoorWindowLifecycleState.GRACE

    def test_sensor_opened_already_paused_noop(self):
        t = transition(DoorWindowLifecycleState.PAUSED_ACTIVE, _ev(DoorWindowFsmEventKind.SENSOR_OPENED))
        assert t.to_state == DoorWindowLifecycleState.PAUSED_ACTIVE

    def test_grace_timer_expired_unreachable_noop(self):
        t = transition(DoorWindowLifecycleState.PAUSED_ACTIVE, _ev(DoorWindowFsmEventKind.GRACE_TIMER_EXPIRED))
        assert t.to_state == DoorWindowLifecycleState.PAUSED_ACTIVE

    def test_sync_reconcile_grace_active_upgrades_to_paused_during_grace(self):
        """Issue #660: _sync_reconcile_next_state() previously never checked live
        grace_active when already in a PAUSED_* state — the FSM's own carried state
        would silently disagree with production once grace started running
        concurrently with an existing pause. SYNC_RECONCILE from PAUSED_ACTIVE/
        PAUSED_IDLE with grace_active=True must now upgrade to PAUSED_DURING_GRACE."""
        t = transition(
            DoorWindowLifecycleState.PAUSED_ACTIVE,
            _ev(DoorWindowFsmEventKind.SYNC_RECONCILE, grace_active=True),
        )
        assert t.to_state == DoorWindowLifecycleState.PAUSED_DURING_GRACE

    def test_sync_reconcile_grace_active_upgrades_from_paused_idle(self):
        t = transition(
            DoorWindowLifecycleState.PAUSED_IDLE,
            _ev(DoorWindowFsmEventKind.SYNC_RECONCILE, grace_active=True),
        )
        assert t.to_state == DoorWindowLifecycleState.PAUSED_DURING_GRACE

    def test_sync_reconcile_grace_inactive_stays_paused_active(self):
        """Regression guard: grace_active=False must not change the outcome —
        SYNC_RECONCILE from a plain paused state with no grace running is still a
        no-op (the pre-existing behavior for this origin state)."""
        t = transition(
            DoorWindowLifecycleState.PAUSED_ACTIVE,
            _ev(DoorWindowFsmEventKind.SYNC_RECONCILE, grace_active=False),
        )
        assert t.to_state == DoorWindowLifecycleState.PAUSED_ACTIVE

    def test_sync_reconcile_grace_inactive_stays_paused_idle(self):
        t = transition(
            DoorWindowLifecycleState.PAUSED_IDLE,
            _ev(DoorWindowFsmEventKind.SYNC_RECONCILE, grace_active=False),
        )
        assert t.to_state == DoorWindowLifecycleState.PAUSED_IDLE

    def test_nat_vent_reactivated_lands_on_normal(self):
        """Issue #660 Step 3: the 8th event kind. From a plain paused state, nat-vent
        reactivating always lands on NORMAL (origin has no grace by definition)."""
        t = transition(
            DoorWindowLifecycleState.PAUSED_ACTIVE,
            _ev(DoorWindowFsmEventKind.PAUSED_NAT_VENT_REACTIVATED),
        )
        assert t.to_state == DoorWindowLifecycleState.NORMAL

    def test_nat_vent_reactivated_lands_on_normal_from_idle(self):
        t = transition(
            DoorWindowLifecycleState.PAUSED_IDLE,
            _ev(DoorWindowFsmEventKind.PAUSED_NAT_VENT_REACTIVATED),
        )
        assert t.to_state == DoorWindowLifecycleState.NORMAL


class TestFromGrace:
    def test_sensor_opened_outdoor_too_warm_suppressed(self):
        t = transition(DoorWindowLifecycleState.GRACE, _ev(DoorWindowFsmEventKind.SENSOR_OPENED, outdoor=90.0))
        assert t.to_state == DoorWindowLifecycleState.GRACE
        assert t.outcome == "grace_suppressed"

    def test_sensor_opened_real_gate_fails_suppressed_not_composite_state(self):
        # Issue #655 fix: the grace guard is now gated on the real reactivation gate
        # (nat_vent_gate_entered), not an outdoor-only proxy. outdoor==indoor (76==76)
        # means the real gate fails (not strictly less), so this must stay suppressed
        # — it must NOT fall through to a composite paused state, even though outdoor
        # alone (76 < threshold 80) would have looked "cool enough" under the old,
        # since-removed proxy check this scenario used to exercise.
        t = transition(
            DoorWindowLifecycleState.GRACE,
            _ev(DoorWindowFsmEventKind.SENSOR_OPENED, outdoor=76.0, indoor=76.0, hvac_mode="cool"),
        )
        assert t.to_state == DoorWindowLifecycleState.GRACE
        assert t.outcome == "grace_suppressed"

    def test_sensor_opened_falls_through_to_nat_vent_eligible_stays_grace(self):
        t = transition(
            DoorWindowLifecycleState.GRACE,
            _ev(DoorWindowFsmEventKind.SENSOR_OPENED, outdoor=60.0, indoor=76.0),
        )
        assert t.to_state == DoorWindowLifecycleState.GRACE
        assert t.outcome == "nat_vent_eligible"

    def test_nat_vent_exited_sensor_still_open_to_composite_state(self):
        t = transition(DoorWindowLifecycleState.GRACE, _ev(DoorWindowFsmEventKind.NAT_VENT_EXITED_SENSOR_STILL_OPEN))
        assert t.to_state == DoorWindowLifecycleState.PAUSED_DURING_GRACE

    def test_grace_timer_expired_planned_window_clears(self):
        t = transition(
            DoorWindowLifecycleState.GRACE,
            _ev(DoorWindowFsmEventKind.GRACE_TIMER_EXPIRED, within_planned_window=True),
        )
        assert t.to_state == DoorWindowLifecycleState.NORMAL
        assert t.outcome == "clear_planned_window"

    def test_grace_timer_expired_re_pause_nat_vent_ineligible(self):
        t = transition(
            DoorWindowLifecycleState.GRACE,
            _ev(
                DoorWindowFsmEventKind.GRACE_TIMER_EXPIRED,
                any_sensor_open=True,
                outdoor=90.0,
                hvac_mode="cool",
            ),
        )
        assert t.to_state == DoorWindowLifecycleState.PAUSED_ACTIVE
        assert t.outcome == "re_pause"

    def test_grace_timer_expired_re_pause_nat_vent_eligible_goes_normal(self):
        t = transition(
            DoorWindowLifecycleState.GRACE,
            _ev(
                DoorWindowFsmEventKind.GRACE_TIMER_EXPIRED,
                any_sensor_open=True,
                outdoor=60.0,
                indoor=76.0,
            ),
        )
        assert t.to_state == DoorWindowLifecycleState.NORMAL
        assert t.outcome == "re_pause"

    def test_grace_timer_expired_adopt_override(self):
        t = transition(
            DoorWindowLifecycleState.GRACE,
            _ev(
                DoorWindowFsmEventKind.GRACE_TIMER_EXPIRED,
                any_sensor_open=False,
                grace_source="manual",
                override_matches_current_decision=True,
            ),
        )
        assert t.to_state == DoorWindowLifecycleState.NORMAL
        assert t.outcome == "adopt_override"

    def test_grace_timer_expired_clear_normal(self):
        t = transition(
            DoorWindowLifecycleState.GRACE,
            _ev(DoorWindowFsmEventKind.GRACE_TIMER_EXPIRED, any_sensor_open=False),
        )
        assert t.to_state == DoorWindowLifecycleState.NORMAL
        assert t.outcome == "clear_normal"

    def test_manual_override_during_pause_noop_from_grace(self):
        t = transition(DoorWindowLifecycleState.GRACE, _ev(DoorWindowFsmEventKind.MANUAL_OVERRIDE_DURING_PAUSE))
        assert t.to_state == DoorWindowLifecycleState.GRACE


class TestFromPausedDuringGrace:
    def test_grace_timer_expired_re_pause_lands_on_paused_idle_when_mode_off(self):
        t = transition(
            DoorWindowLifecycleState.PAUSED_DURING_GRACE,
            _ev(DoorWindowFsmEventKind.GRACE_TIMER_EXPIRED, any_sensor_open=True, hvac_mode="off"),
        )
        assert t.to_state == DoorWindowLifecycleState.PAUSED_IDLE

    def test_grace_timer_expired_planned_window_still_leaves_pause(self):
        t = transition(
            DoorWindowLifecycleState.PAUSED_DURING_GRACE,
            _ev(
                DoorWindowFsmEventKind.GRACE_TIMER_EXPIRED,
                within_planned_window=True,
                hvac_mode="cool",
            ),
        )
        assert t.to_state == DoorWindowLifecycleState.PAUSED_ACTIVE
        assert t.outcome == "clear_planned_window"

    def test_all_sensors_closed_lands_on_grace(self):
        t = transition(DoorWindowLifecycleState.PAUSED_DURING_GRACE, _ev(DoorWindowFsmEventKind.ALL_SENSORS_CLOSED))
        assert t.to_state == DoorWindowLifecycleState.GRACE

    def test_dashboard_resume_lands_on_grace(self):
        t = transition(DoorWindowLifecycleState.PAUSED_DURING_GRACE, _ev(DoorWindowFsmEventKind.DASHBOARD_RESUME))
        assert t.to_state == DoorWindowLifecycleState.GRACE

    def test_manual_override_during_pause_lands_on_grace(self):
        t = transition(
            DoorWindowLifecycleState.PAUSED_DURING_GRACE,
            _ev(DoorWindowFsmEventKind.MANUAL_OVERRIDE_DURING_PAUSE),
        )
        assert t.to_state == DoorWindowLifecycleState.GRACE

    def test_sensor_opened_noop(self):
        t = transition(DoorWindowLifecycleState.PAUSED_DURING_GRACE, _ev(DoorWindowFsmEventKind.SENSOR_OPENED))
        assert t.to_state == DoorWindowLifecycleState.PAUSED_DURING_GRACE

    def test_nat_vent_reactivated_lands_on_grace(self):
        """Issue #660 Step 3: from PAUSED_DURING_GRACE, nat-vent reactivating clears
        the pause but leaves grace running (untouched) -- lands on plain GRACE."""
        t = transition(
            DoorWindowLifecycleState.PAUSED_DURING_GRACE,
            _ev(DoorWindowFsmEventKind.PAUSED_NAT_VENT_REACTIVATED),
        )
        assert t.to_state == DoorWindowLifecycleState.GRACE
