"""Tests for Issue #731 Phase 3: fan/WHF handler-triggered FSM wiring.

Scope discipline (matches nat_vent_fsm.py's test file precedent, cited in its
own docstring): this file proves WIRING correctness only — that transition()
calls the right pure function for the right event kind, folds its outcome
into the right composed-state axis, and populates only the relevant
shell-directive fields (asserting the rest stay None). It does NOT re-prove
the 6 imported pure decision functions' own logic (rate-limit boundary math,
drift-tick-count progression, cycle-on/off thresholds, thermostat-check
priority order) — those already have their own exhaustive test files
(test_fan_toggle_rate_limit.py, and the pre-existing tests for
fan_drift_reconciliation.py/desired_state.py/fan_thermostat_decision.py).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from custom_components.climate_advisor.desired_state import FanCycleOutcome
from custom_components.climate_advisor.fan_drift_reconciliation import FanDriftOutcome
from custom_components.climate_advisor.fan_fsm import (
    FanFsmEvent,
    FanFsmEventKind,
    FanFsmInputs,
    _derive,
    transition,
)
from custom_components.climate_advisor.fan_lifecycle import (
    FanCyclingState,
    FanLifecycleState,
    FanOverrideState,
    FanPhysicalState,
    FanRateLimitState,
    WhfHvacOwnership,
)
from custom_components.climate_advisor.fan_thermostat_decision import FanThermostatOutcome
from custom_components.climate_advisor.fan_toggle_rate_limit import FanToggleRateLimitOutcome

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

_IDLE = FanLifecycleState.initial()

_ACTIVE_STATE = FanLifecycleState(
    physical=FanPhysicalState.ON,
    override=FanOverrideState.NONE,
    cycling=FanCyclingState.IDLE,
    hvac_ownership=WhfHvacOwnership.NONE,
    rate_limit=FanRateLimitState.NOT_DEFERRED,
)


def _inputs(**overrides) -> FanFsmInputs:
    base = dict(
        fan_active=False,
        fan_drift_tick_count=0,
        fan_override_active=False,
        fan_remote_timer_hours=None,
        fan_min_runtime_active=False,
        fan_mode="whole_house_fan",
        pre_fan_hvac_mode=None,
        fan_rate_limited_until=None,
        fan_rate_limited_direction=None,
        now=_NOW,
    )
    base.update(overrides)
    return FanFsmInputs(**base)


def _assert_only_populated(t, *populated_fields: str) -> None:
    """Assert every shell-directive field is None except the ones named."""
    all_shell_fields = (
        "drift_outcome",
        "next_drift_tick_count",
        "cycle_outcome",
        "cycle_delay_seconds",
        "cycle_should_deactivate",
        "thermo_backstop_should_be_armed",
        "thermostat_outcome",
        "rate_limit_outcome",
        "rate_limit_applies_at",
    )
    for field in all_shell_fields:
        value = getattr(t, field)
        if field in populated_fields:
            assert value is not None, f"expected {field} to be populated, was None"
        else:
            assert value is None, f"expected {field} to stay None, got {value!r}"


class TestActivateRequested:
    def test_allow_clears_stale_rate_limit_and_leaves_fan_active_unchanged(self) -> None:
        inputs = _inputs(
            last_toggle_command_time=_NOW - timedelta(seconds=1000),
            fan_active=False,
        )
        t = transition(_IDLE, FanFsmEvent(FanFsmEventKind.ACTIVATE_REQUESTED, inputs))
        assert t.rate_limit_outcome is FanToggleRateLimitOutcome.ALLOW
        assert t.rate_limit_applies_at is None
        assert t.to_state.rate_limit == FanRateLimitState.NOT_DEFERRED
        assert t.to_state.physical == FanPhysicalState.OFF  # unchanged — not this FSM's job
        _assert_only_populated(t, "rate_limit_outcome")

    def test_defer_new_sets_deferred_activate_axis(self) -> None:
        last = _NOW - timedelta(seconds=60)
        inputs = _inputs(last_toggle_command_time=last, toggle_min_interval_s=300.0)
        t = transition(_IDLE, FanFsmEvent(FanFsmEventKind.ACTIVATE_REQUESTED, inputs))
        assert t.rate_limit_outcome is FanToggleRateLimitOutcome.DEFER_NEW
        assert t.rate_limit_applies_at == last + timedelta(seconds=300.0)
        assert t.to_state.rate_limit == FanRateLimitState.DEFERRED_ACTIVATE
        _assert_only_populated(t, "rate_limit_outcome", "rate_limit_applies_at")

    def test_defer_duplicate_still_reports_outcome(self) -> None:
        last = _NOW - timedelta(seconds=60)
        applies_at = last + timedelta(seconds=300.0)
        inputs = _inputs(
            last_toggle_command_time=last,
            fan_rate_limited_until=applies_at,
            fan_rate_limited_direction="activate",
        )
        t = transition(_IDLE, FanFsmEvent(FanFsmEventKind.ACTIVATE_REQUESTED, inputs))
        assert t.rate_limit_outcome is FanToggleRateLimitOutcome.DEFER_DUPLICATE
        assert t.to_state.rate_limit == FanRateLimitState.DEFERRED_ACTIVATE


class TestDeactivateRequested:
    def test_allow_clears_stale_rate_limit(self) -> None:
        inputs = _inputs(
            last_toggle_command_time=_NOW - timedelta(seconds=1000),
            fan_active=True,
        )
        t = transition(_ACTIVE_STATE, FanFsmEvent(FanFsmEventKind.DEACTIVATE_REQUESTED, inputs))
        assert t.rate_limit_outcome is FanToggleRateLimitOutcome.ALLOW
        assert t.to_state.rate_limit == FanRateLimitState.NOT_DEFERRED
        assert t.to_state.physical == FanPhysicalState.ON  # unchanged — not this FSM's job
        _assert_only_populated(t, "rate_limit_outcome")

    def test_defer_new_sets_deferred_deactivate_axis(self) -> None:
        last = _NOW - timedelta(seconds=60)
        inputs = _inputs(last_toggle_command_time=last, fan_active=True)
        t = transition(_ACTIVE_STATE, FanFsmEvent(FanFsmEventKind.DEACTIVATE_REQUESTED, inputs))
        assert t.rate_limit_outcome is FanToggleRateLimitOutcome.DEFER_NEW
        assert t.to_state.rate_limit == FanRateLimitState.DEFERRED_DEACTIVATE
        _assert_only_populated(t, "rate_limit_outcome", "rate_limit_applies_at")


class TestDriftTick:
    def test_reset_when_fan_inactive(self) -> None:
        inputs = _inputs(fan_active=False, fan_drift_tick_count=1)
        t = transition(_IDLE, FanFsmEvent(FanFsmEventKind.DRIFT_TICK, inputs))
        assert t.drift_outcome is FanDriftOutcome.RESET
        assert t.next_drift_tick_count == 0
        assert t.to_state.physical == FanPhysicalState.OFF
        _assert_only_populated(t, "drift_outcome", "next_drift_tick_count")

    def test_awaiting_increments_tick_count_stays_on_drift_suspected(self) -> None:
        inputs = _inputs(
            fan_active=True,
            fan_mode="whole_house_fan",
            fan_drift_tick_count=0,
            recent_fan_command=False,
            physical_state_available=True,
            physical_on=False,
        )
        t = transition(_ACTIVE_STATE, FanFsmEvent(FanFsmEventKind.DRIFT_TICK, inputs))
        assert t.drift_outcome is FanDriftOutcome.AWAITING
        assert t.next_drift_tick_count == 1
        assert t.to_state.physical == FanPhysicalState.ON_DRIFT_SUSPECTED
        assert t.to_state.hvac_ownership == _ACTIVE_STATE.hvac_ownership  # unrelated axis untouched

    def test_correct_confirms_drift_and_flips_fan_active_false(self) -> None:
        inputs = _inputs(
            fan_active=True,
            fan_mode="whole_house_fan",
            fan_drift_tick_count=1,  # one AWAITING tick already recorded
            recent_fan_command=False,
            physical_state_available=True,
            physical_on=False,
        )
        t = transition(_ACTIVE_STATE, FanFsmEvent(FanFsmEventKind.DRIFT_TICK, inputs))
        assert t.drift_outcome is FanDriftOutcome.CORRECT
        assert t.next_drift_tick_count == 0
        assert t.to_state.physical == FanPhysicalState.OFF
        assert t.changed

    def test_noop_when_archetype_not_applicable(self) -> None:
        inputs = _inputs(fan_active=True, fan_mode="hvac_fan", fan_drift_tick_count=0)
        t = transition(_ACTIVE_STATE, FanFsmEvent(FanFsmEventKind.DRIFT_TICK, inputs))
        assert t.drift_outcome is FanDriftOutcome.NOOP
        assert t.next_drift_tick_count == 0
        assert not t.changed


class TestMinRuntimeCycleOn:
    def test_disabled_leaves_state_unchanged(self) -> None:
        inputs = _inputs(fan_min_runtime_minutes=0.0, fan_mode="whole_house_fan")
        t = transition(_IDLE, FanFsmEvent(FanFsmEventKind.MIN_RUNTIME_CYCLE_ON, inputs))
        assert t.cycle_outcome is FanCycleOutcome.DISABLED
        assert t.cycle_delay_seconds is None
        assert not t.changed
        _assert_only_populated(t, "cycle_outcome")

    def test_override_suspended_reports_outcome_without_effective_input_substitution(self) -> None:
        inputs = _inputs(fan_min_runtime_minutes=20.0, fan_override_active=True)
        t = transition(_IDLE, FanFsmEvent(FanFsmEventKind.MIN_RUNTIME_CYCLE_ON, inputs))
        assert t.cycle_outcome is FanCycleOutcome.OVERRIDE_SUSPENDED
        # No ACTIVATE_* outcome fired, so to_state is a straight re-derivation of the
        # given snapshot (which already carries fan_override_active=True) — not a
        # no-op relative to from_state, which was constructed without that flag.
        assert t.to_state == _derive(inputs)

    def test_activate_always_on_sets_fan_active_and_cycling(self) -> None:
        inputs = _inputs(fan_min_runtime_minutes=60.0, fan_active=False)
        t = transition(_IDLE, FanFsmEvent(FanFsmEventKind.MIN_RUNTIME_CYCLE_ON, inputs))
        assert t.cycle_outcome is FanCycleOutcome.ACTIVATE_ALWAYS_ON
        assert t.cycle_delay_seconds is None
        assert t.to_state.physical == FanPhysicalState.ON
        assert t.to_state.cycling == FanCyclingState.ACTIVE
        _assert_only_populated(t, "cycle_outcome")

    def test_activate_with_off_timer_sets_fan_active_and_delay(self) -> None:
        inputs = _inputs(fan_min_runtime_minutes=20.0, fan_active=False)
        t = transition(_IDLE, FanFsmEvent(FanFsmEventKind.MIN_RUNTIME_CYCLE_ON, inputs))
        assert t.cycle_outcome is FanCycleOutcome.ACTIVATE_WITH_OFF_TIMER
        assert t.cycle_delay_seconds is not None
        assert t.to_state.physical == FanPhysicalState.ON
        assert t.to_state.cycling == FanCyclingState.ACTIVE
        _assert_only_populated(t, "cycle_outcome", "cycle_delay_seconds")

    def test_retry_later_leaves_state_unchanged(self) -> None:
        inputs = _inputs(fan_min_runtime_minutes=20.0, fan_active=True, fan_min_runtime_active=False)
        t = transition(_ACTIVE_STATE, FanFsmEvent(FanFsmEventKind.MIN_RUNTIME_CYCLE_ON, inputs))
        assert t.cycle_outcome is FanCycleOutcome.RETRY_LATER
        assert t.cycle_delay_seconds == 60.0 * 60.0
        assert not t.changed


class TestMinRuntimeCycleOff:
    def test_should_deactivate_true_clears_fan_active_and_cycling(self) -> None:
        active_cycling_state = FanLifecycleState(
            physical=FanPhysicalState.ON,
            override=FanOverrideState.NONE,
            cycling=FanCyclingState.ACTIVE,
            hvac_ownership=WhfHvacOwnership.NONE,
            rate_limit=FanRateLimitState.NOT_DEFERRED,
        )
        inputs = _inputs(fan_active=True, fan_min_runtime_active=True, fan_min_runtime_minutes=20.0)
        t = transition(active_cycling_state, FanFsmEvent(FanFsmEventKind.MIN_RUNTIME_CYCLE_OFF, inputs))
        assert t.cycle_should_deactivate is True
        assert t.cycle_delay_seconds is not None
        assert t.to_state.physical == FanPhysicalState.OFF
        assert t.to_state.cycling == FanCyclingState.IDLE
        _assert_only_populated(t, "cycle_delay_seconds", "cycle_should_deactivate")

    def test_should_deactivate_false_leaves_state_unchanged(self) -> None:
        inputs = _inputs(fan_active=False, fan_min_runtime_active=False, fan_min_runtime_minutes=20.0)
        t = transition(_IDLE, FanFsmEvent(FanFsmEventKind.MIN_RUNTIME_CYCLE_OFF, inputs))
        assert t.cycle_should_deactivate is False
        assert t.cycle_delay_seconds is not None
        assert not t.changed


class TestThermoBackstopTick:
    def test_fan_running_arms_backstop_state_unchanged(self) -> None:
        inputs = _inputs(fan_running=True, fan_active=True)
        t = transition(_ACTIVE_STATE, FanFsmEvent(FanFsmEventKind.THERMO_BACKSTOP_TICK, inputs))
        assert t.thermo_backstop_should_be_armed is True
        assert not t.changed
        _assert_only_populated(t, "thermo_backstop_should_be_armed")

    def test_fan_not_running_does_not_arm_backstop(self) -> None:
        inputs = _inputs(fan_running=False, fan_active=False)
        t = transition(_IDLE, FanFsmEvent(FanFsmEventKind.THERMO_BACKSTOP_TICK, inputs))
        assert t.thermo_backstop_should_be_armed is False
        assert not t.changed


class TestThermostatCheckTick:
    def test_keep_never_changes_state(self) -> None:
        inputs = _inputs(
            fan_active=True,
            indoor=70.0,
            outdoor=60.0,
            comfort_heat_raw=65.0,
            sleep_heat=65.0,
            hysteresis=1.0,
        )
        t = transition(_ACTIVE_STATE, FanFsmEvent(FanFsmEventKind.THERMOSTAT_CHECK_TICK, inputs))
        assert t.thermostat_outcome is FanThermostatOutcome.KEEP
        assert not t.changed
        _assert_only_populated(t, "thermostat_outcome")

    def test_stop_outcome_reports_outcome_but_never_changes_state(self) -> None:
        # Check 1: outdoor >= indoor -> stop-direction outcome. This FSM deliberately
        # does not project it onto to_state (see fan_fsm.py's own docstring on this
        # event kind) — only the outcome is surfaced.
        inputs = _inputs(
            fan_active=True,
            indoor=70.0,
            outdoor=75.0,
            comfort_heat_raw=65.0,
            sleep_heat=65.0,
            hysteresis=1.0,
            natural_vent_active=True,
        )
        t = transition(_ACTIVE_STATE, FanFsmEvent(FanFsmEventKind.THERMOSTAT_CHECK_TICK, inputs))
        assert t.thermostat_outcome is FanThermostatOutcome.STOP_VIA_NAT_VENT_EXIT
        assert not t.changed
        assert t.to_state == _derive(inputs)


class TestGroupOnePureCallerAlreadyDecided:
    """Every kind in this group must never call a pure fn and must never
    populate any shell-directive field — only re-derive against the given
    (already post-change) snapshot."""

    def test_startup_reconcile_reflects_given_snapshot(self) -> None:
        inputs = _inputs(fan_active=True, fan_mode="whole_house_fan")
        t = transition(_IDLE, FanFsmEvent(FanFsmEventKind.STARTUP_RECONCILE, inputs))
        assert t.to_state.physical == FanPhysicalState.ON
        _assert_only_populated(t)

    def test_manual_override_detected_reflects_given_snapshot(self) -> None:
        inputs = _inputs(fan_override_active=True, fan_remote_timer_hours=8.0)
        t = transition(_IDLE, FanFsmEvent(FanFsmEventKind.MANUAL_OVERRIDE_DETECTED, inputs))
        assert t.to_state.override == FanOverrideState.ACTIVE_REMOTE_TIMER
        _assert_only_populated(t)

    def test_override_cleared_reflects_given_snapshot(self) -> None:
        inputs = _inputs(fan_override_active=False)
        t = transition(_ACTIVE_STATE, FanFsmEvent(FanFsmEventKind.OVERRIDE_CLEARED, inputs))
        assert t.to_state.override == FanOverrideState.NONE
        _assert_only_populated(t)

    def test_user_fan_off_reflects_given_snapshot(self) -> None:
        inputs = _inputs(fan_active=False)
        t = transition(_ACTIVE_STATE, FanFsmEvent(FanFsmEventKind.USER_FAN_OFF, inputs))
        assert t.to_state.physical == FanPhysicalState.OFF
        _assert_only_populated(t)

    def test_timer_boundary_settle_reflects_given_snapshot(self) -> None:
        inputs = _inputs(fan_active=False)
        t = transition(_ACTIVE_STATE, FanFsmEvent(FanFsmEventKind.TIMER_BOUNDARY_SETTLE, inputs))
        assert t.to_state.physical == FanPhysicalState.OFF
        _assert_only_populated(t)

    def test_flags_cleared_for_grace_reflects_given_snapshot(self) -> None:
        inputs = _inputs(fan_active=False)
        t = transition(_ACTIVE_STATE, FanFsmEvent(FanFsmEventKind.FLAGS_CLEARED_FOR_GRACE, inputs))
        assert t.to_state.physical == FanPhysicalState.OFF
        _assert_only_populated(t)

    def test_min_runtime_cycle_stopped_reflects_given_snapshot(self) -> None:
        inputs = _inputs(fan_min_runtime_active=False)
        t = transition(_ACTIVE_STATE, FanFsmEvent(FanFsmEventKind.MIN_RUNTIME_CYCLE_STOPPED, inputs))
        assert t.to_state.cycling == FanCyclingState.IDLE
        _assert_only_populated(t)

    def test_whf_suppression_requested_reflects_given_snapshot(self) -> None:
        inputs = _inputs(fan_mode="whole_house_fan", pre_fan_hvac_mode="heat")
        t = transition(_IDLE, FanFsmEvent(FanFsmEventKind.WHF_SUPPRESSION_REQUESTED, inputs))
        assert t.to_state.hvac_ownership == WhfHvacOwnership.SUPPRESSING
        _assert_only_populated(t)

    def test_whf_release_requested_reflects_given_snapshot(self) -> None:
        inputs = _inputs(fan_mode="whole_house_fan", pre_fan_hvac_mode=None)
        suppressing_state = FanLifecycleState(
            physical=FanPhysicalState.OFF,
            override=FanOverrideState.NONE,
            cycling=FanCyclingState.IDLE,
            hvac_ownership=WhfHvacOwnership.SUPPRESSING,
            rate_limit=FanRateLimitState.NOT_DEFERRED,
        )
        t = transition(suppressing_state, FanFsmEvent(FanFsmEventKind.WHF_RELEASE_REQUESTED, inputs))
        assert t.to_state.hvac_ownership == WhfHvacOwnership.NONE
        _assert_only_populated(t)


class TestTransitionChangedProperty:
    def test_changed_false_when_states_equal(self) -> None:
        inputs = _inputs()
        t = transition(_IDLE, FanFsmEvent(FanFsmEventKind.STARTUP_RECONCILE, inputs))
        assert t.to_state == _IDLE
        assert not t.changed

    def test_changed_true_when_states_differ(self) -> None:
        inputs = _inputs(fan_active=True)
        t = transition(_IDLE, FanFsmEvent(FanFsmEventKind.STARTUP_RECONCILE, inputs))
        assert t.changed


# Relocated from tests/test_shadow_engine_coverage.py (Issue #757 Phase 6 Step 8 —
# that file's shadow-engine-registry tests were removed along with the dual-engine
# shell; this registry-enforcement test is independent of the shadow engine and
# still applies). Same registry-enforcement shape as the former
# _OVERRIDE_GRACE_EVENT_KIND_REGISTRY/_DOOR_WINDOW_EVENT_KIND_REGISTRY (both
# removed, Issue #757 Phase 6 Steps 3/4), for FanFsmEventKind. Every real fan/WHF
# dispatch site lives INSIDE AutomationEngine itself (automation.py), not in
# coordinator.py/api.py — fan_fsm.py's own module docstring documents all 16
# members as "one per real call site read in full for this phase", each a method
# on AutomationEngine. So this registry's scan target is automation.py.
#
# "unreachable: <reason>" is for FSM event kinds that exist in the enum but are
# deliberately never dispatched from a real call site (documented, not silently missing).
_AUTOMATION_PY = Path(__file__).parent.parent / "custom_components" / "climate_advisor" / "automation.py"

_FAN_FSM_EVENT_KIND_REGISTRY: dict[str, str] = {
    "ACTIVATE_REQUESTED": "reachable",
    "DEACTIVATE_REQUESTED": "reachable",
    # Issue #731 Phase 5: reconcile_fan_on_startup()'s "fan is off" write group spans
    # TWO independent lifecycles — _fan_active/_fan_on_since (fan-lifecycle, owned by
    # _apply_fan_fsm_state()) and _natural_vent_active/_nat_vent_soft_start (nat-vent's
    # own lifecycle, which _apply_fan_fsm_state() does not and should not own). Routing
    # this write group through the dispatcher would silently drop the nat-vent-side flag
    # changes (the FSM branch would apply only the fan-side quarter of this reconcile
    # decision) — a correctness regression, not a gap in wiring effort. Stays a direct
    # write; see automation.py's own comment at this call site for the full rationale.
    "STARTUP_RECONCILE": (
        "unreachable: reconcile_fan_on_startup()'s write group spans nat-vent's own "
        "lifecycle fields, which _apply_fan_fsm_state() doesn't own — dispatching would "
        "silently drop the nat-vent-side half of the decision"
    ),
    "MANUAL_OVERRIDE_DETECTED": "reachable",
    "OVERRIDE_CLEARED": "reachable",
    # Issue #731 Phase 5: on_fan_turned_off()'s normal fan-off path is deliberately left
    # without its own USER_FAN_OFF dispatch — its entire flag-clearing effect IS
    # _clear_fan_flags_and_start_grace() (FLAGS_CLEARED_FOR_GRACE's real dispatch site),
    # so a second dispatch here would report the same net state change twice for one
    # logical event (double-dispatch), not add real coverage.
    "USER_FAN_OFF": (
        "unreachable: fully delegated to FLAGS_CLEARED_FOR_GRACE to avoid "
        "double-dispatching the same logical event — see fan_fsm.py's own "
        "USER_FAN_OFF/FLAGS_CLEARED_FOR_GRACE docstring split"
    ),
    "TIMER_BOUNDARY_SETTLE": "reachable",
    "FLAGS_CLEARED_FOR_GRACE": "reachable",
    "MIN_RUNTIME_CYCLE_ON": "reachable",
    "MIN_RUNTIME_CYCLE_OFF": "reachable",
    "MIN_RUNTIME_CYCLE_STOPPED": "reachable",
    "DRIFT_TICK": "reachable",
    "THERMO_BACKSTOP_TICK": "reachable",
    "THERMOSTAT_CHECK_TICK": "reachable",
    "WHF_SUPPRESSION_REQUESTED": "reachable",
    "WHF_RELEASE_REQUESTED": "reachable",
}


class TestFanFsmEventCoverage:
    def test_every_event_kind_is_registered(self) -> None:
        from custom_components.climate_advisor.fan_fsm import FanFsmEventKind

        all_kinds = {member.name for member in FanFsmEventKind}
        unregistered = all_kinds - set(_FAN_FSM_EVENT_KIND_REGISTRY)
        assert not unregistered, (
            f"New FanFsmEventKind member(s) aren't in _FAN_FSM_EVENT_KIND_REGISTRY: "
            f'{sorted(unregistered)}. Classify each as "reachable" (and wire a real '
            f'dispatch site in automation.py) or "unreachable: <reason>" — see Issue #731.'
        )

    def test_registry_entries_reference_real_members(self) -> None:
        from custom_components.climate_advisor.fan_fsm import FanFsmEventKind

        all_kinds = {member.name for member in FanFsmEventKind}
        unknown = set(_FAN_FSM_EVENT_KIND_REGISTRY) - all_kinds
        assert not unknown, (
            f"Registry references FanFsmEventKind member(s) that no longer exist "
            f"(renamed or removed?): {sorted(unknown)}. Update the registry."
        )

    def test_every_reachable_kind_has_a_real_dispatch_site(self) -> None:
        """Positive control: every 'reachable' entry must appear as a direct
        FanFsmEventKind.<X> reference in automation.py — unlike override/grace and
        door/window (dispatched from coordinator.py/api.py via mirror-name-keyed
        dicts), every real fan/WHF dispatch site is a method on AutomationEngine
        itself, so automation.py is the correct — and only — scan target."""
        src = _AUTOMATION_PY.read_text(encoding="utf-8")
        for name, classification in _FAN_FSM_EVENT_KIND_REGISTRY.items():
            if classification != "reachable":
                continue
            member_pattern = re.compile(r"\bFanFsmEventKind\." + re.escape(name) + r"\b")
            assert member_pattern.search(src), (
                f'{name} is marked "reachable" but no direct FanFsmEventKind.{name} '
                f"reference was found in automation.py"
            )

    def test_every_unreachable_kind_has_no_real_dispatch_site(self) -> None:
        """Inverse positive control: an 'unreachable' entry must NOT appear as a
        direct FanFsmEventKind.<X> dispatch reference in automation.py — catches the
        registry claiming a kind is deliberately unwired when a later phase actually
        wired it (the classification going stale in the opposite direction)."""
        src = _AUTOMATION_PY.read_text(encoding="utf-8")
        for name, classification in _FAN_FSM_EVENT_KIND_REGISTRY.items():
            if not classification.startswith("unreachable"):
                continue
            member_pattern = re.compile(r"\bFanFsmEventKind\." + re.escape(name) + r"\b")
            assert not member_pattern.search(src), (
                f'{name} is marked "unreachable" but a direct FanFsmEventKind.{name} '
                f'reference now exists in automation.py — reclassify to "reachable" '
                f"(Issue #731)."
            )

    def test_positive_control_unregistered_kind_is_caught(self) -> None:
        """Proves test_every_event_kind_is_registered actually fails on a genuinely
        unregistered member, not just passing vacuously."""
        all_kinds = {"A_TOTALLY_NEW_KIND_NOT_IN_REGISTRY"}
        unregistered = all_kinds - set(_FAN_FSM_EVENT_KIND_REGISTRY)
        assert unregistered == {"A_TOTALLY_NEW_KIND_NOT_IN_REGISTRY"}
