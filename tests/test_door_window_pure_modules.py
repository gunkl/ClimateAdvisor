"""Unit tests for the 5 pure building blocks of Issue #637 (Block 5 Phase 2:
door/window pause FSM). Each function's input space is small enough for
exhaustive coverage — mirrors the direct-unit-test layer of
``tests/test_nat_vent_lifecycle_state.py``.
"""

from __future__ import annotations

import pytest

from custom_components.climate_advisor.door_window_close_response import (
    DoorCloseResponseInputs,
    DoorCloseResponseOutcome,
    decide_door_close_response,
)
from custom_components.climate_advisor.door_window_grace_expiry import (
    GraceExpiryInputs,
    GraceExpiryOutcome,
    decide_grace_expiry_outcome,
)
from custom_components.climate_advisor.door_window_lifecycle import (
    DoorWindowLifecycleInputs,
    DoorWindowLifecycleState,
    derive_door_window_lifecycle_state,
)
from custom_components.climate_advisor.door_window_open_response import (
    DoorOpenResponse,
    DoorOpenResponseInputs,
    decide_door_open_response,
)
from custom_components.climate_advisor.door_window_pause_entry import (
    PauseEntryInputs,
    PauseEntryOutcome,
    decide_pause_entry,
)


class TestDeriveDoorWindowLifecycleState:
    @pytest.mark.parametrize(
        ("paused_by_door", "already_off", "grace_active", "expected"),
        [
            (False, False, False, DoorWindowLifecycleState.NORMAL),
            (False, True, False, DoorWindowLifecycleState.NORMAL),  # already_off irrelevant unless paused
            (False, False, True, DoorWindowLifecycleState.GRACE),
            (False, True, True, DoorWindowLifecycleState.GRACE),
            (True, False, False, DoorWindowLifecycleState.PAUSED_ACTIVE),
            (True, True, False, DoorWindowLifecycleState.PAUSED_IDLE),
            (True, False, True, DoorWindowLifecycleState.PAUSED_DURING_GRACE),
            (True, True, True, DoorWindowLifecycleState.PAUSED_DURING_GRACE),
        ],
    )
    def test_exhaustive_flag_space(self, paused_by_door, already_off, grace_active, expected):
        result = derive_door_window_lifecycle_state(
            DoorWindowLifecycleInputs(
                paused_by_door=paused_by_door,
                paused_with_hvac_already_off=already_off,
                grace_active=grace_active,
            )
        )
        assert result == expected


class TestDecidePauseEntry:
    @pytest.mark.parametrize(
        ("hvac_mode", "expected"),
        [
            ("heat", PauseEntryOutcome.PAUSE_ACTIVE),
            ("cool", PauseEntryOutcome.PAUSE_ACTIVE),
            ("heat_cool", PauseEntryOutcome.PAUSE_ACTIVE),
            ("off", PauseEntryOutcome.PAUSE_IDLE),
            ("unavailable", PauseEntryOutcome.NOOP_NOT_APPLICABLE),
            ("unknown", PauseEntryOutcome.NOOP_NOT_APPLICABLE),
            (None, PauseEntryOutcome.NOOP_NOT_APPLICABLE),
        ],
    )
    def test_mode_branches(self, hvac_mode, expected):
        assert decide_pause_entry(PauseEntryInputs(hvac_mode=hvac_mode)) == expected


class TestDecideDoorOpenResponse:
    def _inputs(self, **overrides):
        base = dict(
            paused_by_door=False,
            grace_active=False,
            outdoor=70.0,
            comfort_cool=78.0,
            nat_vent_delta=2.0,
            within_planned_window=False,
            nat_vent_gate_entered=False,
        )
        base.update(overrides)
        return DoorOpenResponseInputs(**base)

    def test_already_paused_noop(self):
        result = decide_door_open_response(self._inputs(paused_by_door=True))
        assert result == DoorOpenResponse.ALREADY_PAUSED_NOOP

    def test_grace_active_gate_not_entered_suppressed(self):
        # Issue #655 fix: grace-active fallthrough is gated on the real reactivation
        # gate result, not a coarse outdoor-only proxy — outdoor alone can no longer
        # decide this. A cool outdoor temp with the real gate still False must NOT
        # fall through (the exact premature-pause bug #655 reported).
        result = decide_door_open_response(self._inputs(grace_active=True, outdoor=65.0, nat_vent_gate_entered=False))
        assert result == DoorOpenResponse.GRACE_SUPPRESSED

    def test_grace_active_outdoor_unavailable_suppressed(self):
        result = decide_door_open_response(self._inputs(grace_active=True, outdoor=None))
        assert result == DoorOpenResponse.GRACE_SUPPRESSED

    def test_grace_active_gate_entered_never_falls_through_to_pause(self):
        # Once the shared gate is entered, the same result is reused at the final
        # nat-vent-vs-pause branch below — grace-active fallthrough can only land on
        # NAT_VENT_ELIGIBLE (or PLANNED_WINDOW_NOOP), never PAUSE. This invariant is
        # the fix: #655's bug was reachable exactly because the old coarse proxy and
        # the real gate could disagree, letting a "cool enough" outdoor temp fall
        # through the grace suppression only to still land on PAUSE moments later.
        result = decide_door_open_response(self._inputs(grace_active=True, outdoor=65.0, nat_vent_gate_entered=True))
        assert result == DoorOpenResponse.NAT_VENT_ELIGIBLE

    def test_planned_window_noop(self):
        result = decide_door_open_response(self._inputs(within_planned_window=True))
        assert result == DoorOpenResponse.PLANNED_WINDOW_NOOP

    def test_nat_vent_eligible(self):
        result = decide_door_open_response(self._inputs(nat_vent_gate_entered=True))
        assert result == DoorOpenResponse.NAT_VENT_ELIGIBLE

    def test_default_pause(self):
        result = decide_door_open_response(self._inputs())
        assert result == DoorOpenResponse.PAUSE

    def test_precedence_already_paused_beats_everything(self):
        result = decide_door_open_response(
            self._inputs(paused_by_door=True, grace_active=True, within_planned_window=True, nat_vent_gate_entered=True)
        )
        assert result == DoorOpenResponse.ALREADY_PAUSED_NOOP

    def test_precedence_planned_window_beats_nat_vent(self):
        result = decide_door_open_response(self._inputs(within_planned_window=True, nat_vent_gate_entered=True))
        assert result == DoorOpenResponse.PLANNED_WINDOW_NOOP


class TestDecideDoorCloseResponse:
    def test_not_paused_noop(self):
        result = decide_door_close_response(DoorCloseResponseInputs(paused_by_door=False, pre_pause_mode=None))
        assert result == DoorCloseResponseOutcome.NOOP_NOT_PAUSED

    def test_not_paused_noop_even_with_stale_pre_pause_mode(self):
        result = decide_door_close_response(DoorCloseResponseInputs(paused_by_door=False, pre_pause_mode="heat"))
        assert result == DoorCloseResponseOutcome.NOOP_NOT_PAUSED

    def test_paused_with_pre_pause_mode_restores_and_graces(self):
        result = decide_door_close_response(DoorCloseResponseInputs(paused_by_door=True, pre_pause_mode="cool"))
        assert result == DoorCloseResponseOutcome.RESTORE_AND_GRACE

    def test_paused_idle_no_pre_pause_mode_clears_without_grace(self):
        result = decide_door_close_response(DoorCloseResponseInputs(paused_by_door=True, pre_pause_mode=None))
        assert result == DoorCloseResponseOutcome.CLEAR_NO_GRACE


class TestDecideGraceExpiryOutcome:
    def _inputs(self, **overrides):
        base = dict(
            within_planned_window=False,
            any_sensor_open=False,
            source="manual",
            override_matches_current_decision=False,
        )
        base.update(overrides)
        return GraceExpiryInputs(**base)

    def test_planned_window_clears(self):
        result = decide_grace_expiry_outcome(self._inputs(within_planned_window=True))
        assert result == GraceExpiryOutcome.CLEAR_PLANNED_WINDOW

    def test_sensor_still_open_re_pauses(self):
        result = decide_grace_expiry_outcome(self._inputs(any_sensor_open=True))
        assert result == GraceExpiryOutcome.RE_PAUSE

    def test_planned_window_beats_sensor_open(self):
        result = decide_grace_expiry_outcome(self._inputs(within_planned_window=True, any_sensor_open=True))
        assert result == GraceExpiryOutcome.CLEAR_PLANNED_WINDOW

    def test_manual_source_matching_decision_adopts(self):
        result = decide_grace_expiry_outcome(self._inputs(source="manual", override_matches_current_decision=True))
        assert result == GraceExpiryOutcome.ADOPT_OVERRIDE

    def test_automation_source_never_adopts(self):
        result = decide_grace_expiry_outcome(self._inputs(source="automation", override_matches_current_decision=True))
        assert result == GraceExpiryOutcome.CLEAR_NORMAL

    def test_manual_source_not_matching_clears_normal(self):
        result = decide_grace_expiry_outcome(self._inputs(source="manual", override_matches_current_decision=False))
        assert result == GraceExpiryOutcome.CLEAR_NORMAL

    def test_sensor_open_beats_adopt(self):
        result = decide_grace_expiry_outcome(
            self._inputs(any_sensor_open=True, source="manual", override_matches_current_decision=True)
        )
        assert result == GraceExpiryOutcome.RE_PAUSE
