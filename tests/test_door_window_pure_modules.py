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
    """Issue #1142: the module is the handler's real decision chain (order: paused, protected change, grace,
    temperature gate, fan-off hold, lockout, start veto, start)."""

    def _inputs(self, **overrides):
        base = dict(
            paused_by_door=False,
            manual_change_protected=False,
            grace_active=False,
            grace_ignored=False,
            start_viable=False,
            start_vetoed=False,
            fan_off_hold_active=False,
            reactivation_locked_out=False,
        )
        base.update(overrides)
        return DoorOpenResponseInputs(**base)

    def test_already_paused_noop(self):
        assert decide_door_open_response(self._inputs(paused_by_door=True)) == DoorOpenResponse.ALREADY_PAUSED_NOOP

    def test_protected_occupant_change_is_not_paused(self):
        result = decide_door_open_response(self._inputs(manual_change_protected=True, start_viable=True))
        assert result == DoorOpenResponse.PROTECTED_CHANGE_NOOP

    def test_grace_holds_the_pause_when_no_fan_session_would_start(self):
        result = decide_door_open_response(self._inputs(grace_active=True))
        assert result == DoorOpenResponse.GRACE_SUPPRESSED

    def test_grace_holds_the_pause_when_the_start_would_be_vetoed(self):
        """#1142 D3: 'viable by temperature' but vetoed is NOT a start, so the grace holds."""
        result = decide_door_open_response(self._inputs(grace_active=True, start_vetoed=True))
        assert result == DoorOpenResponse.GRACE_SUPPRESSED

    def test_grace_with_a_start_falls_through_to_the_start(self):
        result = decide_door_open_response(self._inputs(grace_active=True, start_viable=True))
        assert result == DoorOpenResponse.START_NAT_VENT

    def test_a_grace_that_turns_no_hvac_on_never_holds_the_pause(self):
        result = decide_door_open_response(self._inputs(grace_active=True, grace_ignored=True))
        assert result == DoorOpenResponse.PAUSE

    def test_start_when_the_gate_passes(self):
        assert decide_door_open_response(self._inputs(start_viable=True)) == DoorOpenResponse.START_NAT_VENT

    def test_default_pause(self):
        assert decide_door_open_response(self._inputs()) == DoorOpenResponse.PAUSE

    def test_fan_off_hold_beats_a_start(self):
        result = decide_door_open_response(self._inputs(start_viable=True, fan_off_hold_active=True))
        assert result == DoorOpenResponse.PAUSE_FAN_OFF_HOLD

    def test_lockout_beats_a_start_and_a_veto(self):
        for kwargs in ({"start_viable": True}, {"start_vetoed": True}):
            result = decide_door_open_response(self._inputs(reactivation_locked_out=True, **kwargs))
            assert result == DoorOpenResponse.PAUSE_REACTIVATION_LOCKOUT

    def test_vetoed_start_pauses_and_says_why(self):
        result = decide_door_open_response(self._inputs(start_vetoed=True))
        assert result == DoorOpenResponse.PAUSE_START_VETOED

    def test_precedence_already_paused_beats_everything(self):
        result = decide_door_open_response(
            self._inputs(paused_by_door=True, manual_change_protected=True, grace_active=True, start_viable=True)
        )
        assert result == DoorOpenResponse.ALREADY_PAUSED_NOOP

    def test_gate_failure_ignores_hold_and_lockout(self):
        result = decide_door_open_response(self._inputs(fan_off_hold_active=True, reactivation_locked_out=True))
        assert result == DoorOpenResponse.PAUSE

    def test_planned_window_no_longer_exists_as_an_outcome(self):
        """Issue #1211 (A3): the planned window is not a door-open outcome any more."""
        assert not hasattr(DoorOpenResponse, "PLANNED_WINDOW_NOOP")


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
            any_sensor_open=False,
            source="manual",
            override_matches_current_decision=False,
        )
        base.update(overrides)
        return GraceExpiryInputs(**base)

    def test_sensor_still_open_re_pauses(self):
        result = decide_grace_expiry_outcome(self._inputs(any_sensor_open=True))
        assert result == GraceExpiryOutcome.RE_PAUSE

    def test_planned_window_is_not_an_expiry_outcome(self):
        """Issue #1211 (A3): a sensor still open at expiry re-pauses inside the planned period too."""
        assert not hasattr(GraceExpiryOutcome, "CLEAR_PLANNED_WINDOW")

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
