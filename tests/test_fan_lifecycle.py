"""Tests for Issue #731 Phase 1: fan/WHF composed-state derivation.

Exhaustive over each of the five independent axes, plus meaningful cross-product
cases, plus an explicit regression test for the drift-tick-count safety
invariant that ``ON_DRIFT_SUSPECTED`` still means ``fan_active=True`` — a
protection for the "active (unconfirmed)" status string documented in
docs/08-COMPUTATION-REFERENCE.md.
"""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.climate_advisor.fan_lifecycle import (
    FanCyclingState,
    FanLifecycleInputs,
    FanLifecycleState,
    FanOverrideState,
    FanPhysicalState,
    FanRateLimitState,
    WhfHvacOwnership,
    derive_fan_lifecycle_state,
)

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _inputs(
    *,
    fan_active: bool = False,
    fan_drift_tick_count: int = 0,
    fan_override_active: bool = False,
    fan_remote_timer_hours: float | None = None,
    fan_min_runtime_active: bool = False,
    fan_mode: str = "whole_house_fan",
    pre_fan_hvac_mode: str | None = None,
    fan_rate_limited_until: datetime | None = None,
    fan_rate_limited_direction: str | None = None,
    now: datetime = _NOW,
) -> FanLifecycleInputs:
    return FanLifecycleInputs(
        fan_active=fan_active,
        fan_drift_tick_count=fan_drift_tick_count,
        fan_override_active=fan_override_active,
        fan_remote_timer_hours=fan_remote_timer_hours,
        fan_min_runtime_active=fan_min_runtime_active,
        fan_mode=fan_mode,
        pre_fan_hvac_mode=pre_fan_hvac_mode,
        fan_rate_limited_until=fan_rate_limited_until,
        fan_rate_limited_direction=fan_rate_limited_direction,
        now=now,
    )


class TestInitialState:
    def test_initial_is_all_idle(self) -> None:
        assert FanLifecycleState.initial() == FanLifecycleState(
            physical=FanPhysicalState.OFF,
            override=FanOverrideState.NONE,
            cycling=FanCyclingState.IDLE,
            hvac_ownership=WhfHvacOwnership.NONE,
            rate_limit=FanRateLimitState.NOT_DEFERRED,
        )

    def test_default_inputs_derive_to_initial_state(self) -> None:
        assert derive_fan_lifecycle_state(_inputs()) == FanLifecycleState.initial()


class TestPhysicalAxis:
    def test_off_when_fan_not_active(self) -> None:
        state = derive_fan_lifecycle_state(_inputs(fan_active=False, fan_drift_tick_count=1))
        assert state.physical == FanPhysicalState.OFF

    def test_on_when_active_no_drift(self) -> None:
        state = derive_fan_lifecycle_state(_inputs(fan_active=True, fan_drift_tick_count=0))
        assert state.physical == FanPhysicalState.ON

    def test_on_drift_suspected_when_active_and_tick_count_positive(self) -> None:
        state = derive_fan_lifecycle_state(_inputs(fan_active=True, fan_drift_tick_count=1))
        assert state.physical == FanPhysicalState.ON_DRIFT_SUSPECTED

    def test_drift_tick_count_two_still_on_drift_suspected(self) -> None:
        # tick_count reaching 2 (the confirm threshold) is a transient the shell
        # resolves to CORRECT (tick_count resets to 0, fan_active flips False) in
        # the same step — but the pure derivation itself must not special-case the
        # threshold value; any positive count with fan_active=True is drift-suspected.
        state = derive_fan_lifecycle_state(_inputs(fan_active=True, fan_drift_tick_count=2))
        assert state.physical == FanPhysicalState.ON_DRIFT_SUSPECTED

    def test_drift_tick_count_positive_but_fan_inactive_is_off_not_drift(self) -> None:
        # Should not be physically reachable per fan_drift_reconciliation.py's contract
        # (fan_active=False always accompanies tick_count=0 via the RESET outcome), but
        # the derivation must still fail safe: fan_active is the authoritative gate.
        state = derive_fan_lifecycle_state(_inputs(fan_active=False, fan_drift_tick_count=1))
        assert state.physical == FanPhysicalState.OFF

    def test_safety_invariant_drift_suspected_implies_fan_active_input_was_true(self) -> None:
        """Regression test (safety-critical): ON_DRIFT_SUSPECTED must only ever be
        derived from fan_active=True. Protects the "active (unconfirmed)" status
        string — see docs/08-COMPUTATION-REFERENCE.md's Fan Status Values table."""
        inputs = _inputs(fan_active=True, fan_drift_tick_count=1)
        state = derive_fan_lifecycle_state(inputs)
        assert state.physical == FanPhysicalState.ON_DRIFT_SUSPECTED
        assert inputs.fan_active is True


class TestOverrideAxis:
    def test_none_when_not_override_active(self) -> None:
        state = derive_fan_lifecycle_state(_inputs(fan_override_active=False, fan_remote_timer_hours=8.0))
        assert state.override == FanOverrideState.NONE

    def test_active_when_override_no_remote_timer(self) -> None:
        state = derive_fan_lifecycle_state(_inputs(fan_override_active=True, fan_remote_timer_hours=None))
        assert state.override == FanOverrideState.ACTIVE

    def test_active_remote_timer_when_override_and_timer_set(self) -> None:
        state = derive_fan_lifecycle_state(_inputs(fan_override_active=True, fan_remote_timer_hours=8.0))
        assert state.override == FanOverrideState.ACTIVE_REMOTE_TIMER

    def test_active_remote_timer_zero_hours_still_counts_as_set(self) -> None:
        # 0.0 is not None -> still a genuine remote timer selection.
        state = derive_fan_lifecycle_state(_inputs(fan_override_active=True, fan_remote_timer_hours=0.0))
        assert state.override == FanOverrideState.ACTIVE_REMOTE_TIMER


class TestCyclingAxis:
    def test_idle_when_no_cycling_no_override(self) -> None:
        state = derive_fan_lifecycle_state(_inputs(fan_min_runtime_active=False, fan_override_active=False))
        assert state.cycling == FanCyclingState.IDLE

    def test_suspended_when_override_active_and_not_cycling(self) -> None:
        state = derive_fan_lifecycle_state(_inputs(fan_min_runtime_active=False, fan_override_active=True))
        assert state.cycling == FanCyclingState.SUSPENDED

    def test_active_when_min_runtime_active(self) -> None:
        state = derive_fan_lifecycle_state(_inputs(fan_min_runtime_active=True, fan_override_active=False))
        assert state.cycling == FanCyclingState.ACTIVE

    def test_active_takes_precedence_over_override_if_both_somehow_true(self) -> None:
        # Not reachable in production (handle_fan_manual_override() always stops
        # cycling before setting the override flag) but the derivation's stated
        # precedence checks ACTIVE first regardless.
        state = derive_fan_lifecycle_state(_inputs(fan_min_runtime_active=True, fan_override_active=True))
        assert state.cycling == FanCyclingState.ACTIVE


class TestHvacOwnershipAxis:
    def test_none_when_pre_fan_hvac_mode_is_none(self) -> None:
        state = derive_fan_lifecycle_state(_inputs(fan_mode="whole_house_fan", pre_fan_hvac_mode=None))
        assert state.hvac_ownership == WhfHvacOwnership.NONE

    def test_suppressing_when_whole_house_and_pre_fan_hvac_mode_set(self) -> None:
        state = derive_fan_lifecycle_state(_inputs(fan_mode="whole_house_fan", pre_fan_hvac_mode="heat"))
        assert state.hvac_ownership == WhfHvacOwnership.SUPPRESSING

    def test_suppressing_when_both_archetype_and_pre_fan_hvac_mode_set(self) -> None:
        state = derive_fan_lifecycle_state(_inputs(fan_mode="both", pre_fan_hvac_mode="cool"))
        assert state.hvac_ownership == WhfHvacOwnership.SUPPRESSING

    def test_none_when_hvac_fan_archetype_even_with_pre_fan_hvac_mode_set(self) -> None:
        # FAN_MODE_HVAC coexists with the compressor by design (Issue #392 Fix 1) —
        # _whf_owns_hvac() never applies to this archetype.
        state = derive_fan_lifecycle_state(_inputs(fan_mode="hvac_fan", pre_fan_hvac_mode="heat"))
        assert state.hvac_ownership == WhfHvacOwnership.NONE

    def test_none_when_disabled_archetype(self) -> None:
        state = derive_fan_lifecycle_state(_inputs(fan_mode="disabled", pre_fan_hvac_mode="heat"))
        assert state.hvac_ownership == WhfHvacOwnership.NONE


class TestRateLimitAxis:
    def test_not_deferred_when_rate_limited_until_is_none(self) -> None:
        state = derive_fan_lifecycle_state(_inputs(fan_rate_limited_until=None, fan_rate_limited_direction=None))
        assert state.rate_limit == FanRateLimitState.NOT_DEFERRED

    def test_deferred_activate(self) -> None:
        state = derive_fan_lifecycle_state(_inputs(fan_rate_limited_until=_NOW, fan_rate_limited_direction="activate"))
        assert state.rate_limit == FanRateLimitState.DEFERRED_ACTIVATE

    def test_deferred_deactivate(self) -> None:
        state = derive_fan_lifecycle_state(
            _inputs(fan_rate_limited_until=_NOW, fan_rate_limited_direction="deactivate")
        )
        assert state.rate_limit == FanRateLimitState.DEFERRED_DEACTIVATE

    def test_not_deferred_when_until_set_but_direction_unrecognized(self) -> None:
        # Defensive fallback: should not be reachable in production (the two fields
        # are always set together by _fan_toggle_rate_limited()) but fails safe to
        # NOT_DEFERRED rather than raising on an unexpected direction string.
        state = derive_fan_lifecycle_state(_inputs(fan_rate_limited_until=_NOW, fan_rate_limited_direction="sideways"))
        assert state.rate_limit == FanRateLimitState.NOT_DEFERRED


class TestCrossProduct:
    def test_whf_physically_on_under_override_suppressing_hvac_and_rate_limited(self) -> None:
        state = derive_fan_lifecycle_state(
            _inputs(
                fan_active=True,
                fan_drift_tick_count=0,
                fan_override_active=True,
                fan_remote_timer_hours=8.0,
                fan_min_runtime_active=False,
                fan_mode="whole_house_fan",
                pre_fan_hvac_mode="heat",
                fan_rate_limited_until=_NOW,
                fan_rate_limited_direction="deactivate",
            )
        )
        assert state == FanLifecycleState(
            physical=FanPhysicalState.ON,
            override=FanOverrideState.ACTIVE_REMOTE_TIMER,
            cycling=FanCyclingState.SUSPENDED,
            hvac_ownership=WhfHvacOwnership.SUPPRESSING,
            rate_limit=FanRateLimitState.DEFERRED_DEACTIVATE,
        )

    def test_hvac_fan_mode_cycling_no_override_no_suppression(self) -> None:
        state = derive_fan_lifecycle_state(
            _inputs(
                fan_active=True,
                fan_drift_tick_count=0,
                fan_override_active=False,
                fan_min_runtime_active=True,
                fan_mode="hvac_fan",
                pre_fan_hvac_mode=None,
            )
        )
        assert state == FanLifecycleState(
            physical=FanPhysicalState.ON,
            override=FanOverrideState.NONE,
            cycling=FanCyclingState.ACTIVE,
            hvac_ownership=WhfHvacOwnership.NONE,
            rate_limit=FanRateLimitState.NOT_DEFERRED,
        )

    def test_drift_suspected_while_also_rate_limited(self) -> None:
        state = derive_fan_lifecycle_state(
            _inputs(
                fan_active=True,
                fan_drift_tick_count=1,
                fan_rate_limited_until=_NOW,
                fan_rate_limited_direction="activate",
            )
        )
        assert state.physical == FanPhysicalState.ON_DRIFT_SUSPECTED
        assert state.rate_limit == FanRateLimitState.DEFERRED_ACTIVATE
