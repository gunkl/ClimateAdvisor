"""Tests for occupancy_fsm.py (Issue #744) — pure away/vacation dispatch and home
dispatch decisions, extracted from handle_occupancy_away()/handle_occupancy_vacation()/
handle_occupancy_home() in automation.py.
"""

from __future__ import annotations

from custom_components.climate_advisor.occupancy_fsm import (
    AwayVacationInputs,
    AwayVacationOutcome,
    HomeInputs,
    HomeNotifyOutcome,
    decide_away_vacation_dispatch,
    decide_home_dispatch,
)


class TestAwayVacationDispatch:
    def test_paused_by_door_suppresses_regardless_of_override_or_classification(self) -> None:
        decision = decide_away_vacation_dispatch(
            AwayVacationInputs(paused_by_door=True, manual_override_active=True, has_classification=True)
        )
        assert decision.outcome is AwayVacationOutcome.SUPPRESSED_PAUSED
        assert decision.clear_override is False

    def test_no_classification_no_override(self) -> None:
        decision = decide_away_vacation_dispatch(
            AwayVacationInputs(paused_by_door=False, manual_override_active=False, has_classification=False)
        )
        assert decision.outcome is AwayVacationOutcome.NO_CLASSIFICATION
        assert decision.clear_override is False

    def test_no_classification_but_override_still_cleared(self) -> None:
        """Legacy clears the override BEFORE checking classification presence — the
        clear must fire even when the classification-gate later no-ops."""
        decision = decide_away_vacation_dispatch(
            AwayVacationInputs(paused_by_door=False, manual_override_active=True, has_classification=False)
        )
        assert decision.outcome is AwayVacationOutcome.NO_CLASSIFICATION
        assert decision.clear_override is True

    def test_apply_no_override(self) -> None:
        decision = decide_away_vacation_dispatch(
            AwayVacationInputs(paused_by_door=False, manual_override_active=False, has_classification=True)
        )
        assert decision.outcome is AwayVacationOutcome.APPLY
        assert decision.clear_override is False

    def test_apply_with_override_clear(self) -> None:
        decision = decide_away_vacation_dispatch(
            AwayVacationInputs(paused_by_door=False, manual_override_active=True, has_classification=True)
        )
        assert decision.outcome is AwayVacationOutcome.APPLY
        assert decision.clear_override is True


def _home_inputs(**overrides) -> HomeInputs:
    base = dict(
        has_classification=True,
        hvac_mode="cool",
        indoor_temp_f=78.0,
        comfort_f=76.0,
        setback_f=82.0,
        debounce_seconds=3600.0,
        seconds_since_last_notified=None,
    )
    base.update(overrides)
    return HomeInputs(**base)


class TestHomeDispatch:
    def test_no_classification_short_circuits(self) -> None:
        decision = decide_home_dispatch(
            HomeInputs(
                has_classification=False,
                hvac_mode=None,
                indoor_temp_f=None,
                comfort_f=None,
                setback_f=None,
                debounce_seconds=3600.0,
                seconds_since_last_notified=None,
            )
        )
        assert decision.restore is False
        assert decision.notify is HomeNotifyOutcome.NONE

    def test_hvac_off_day_does_not_restore_but_still_notifies(self) -> None:
        decision = decide_home_dispatch(
            _home_inputs(hvac_mode="off", comfort_f=None, setback_f=None, indoor_temp_f=78.0)
        )
        assert decision.restore is False
        assert decision.notify is HomeNotifyOutcome.SEND

    def test_heat_mode_restores(self) -> None:
        decision = decide_home_dispatch(
            _home_inputs(hvac_mode="heat", comfort_f=70.0, setback_f=60.0, indoor_temp_f=65.0)
        )
        assert decision.restore is True

    def test_cool_mode_restores(self) -> None:
        decision = decide_home_dispatch(_home_inputs(hvac_mode="cool"))
        assert decision.restore is True

    def test_near_comfort_suppresses_notification_but_still_restores(self) -> None:
        # indoor=77, comfort=76 (dist=1), setback=82 (dist=5) -> near comfort
        decision = decide_home_dispatch(_home_inputs(indoor_temp_f=77.0, comfort_f=76.0, setback_f=82.0))
        assert decision.restore is True
        assert decision.notify is HomeNotifyOutcome.SUPPRESSED_NEAR_COMFORT

    def test_near_setback_does_not_suppress(self) -> None:
        # indoor=81, comfort=76 (dist=5), setback=82 (dist=1) -> nearer setback, no suppress
        decision = decide_home_dispatch(_home_inputs(indoor_temp_f=81.0, comfort_f=76.0, setback_f=82.0))
        assert decision.notify is HomeNotifyOutcome.SEND

    def test_equal_distance_does_not_suppress(self) -> None:
        # strict < required; equal distance falls through to debounce/send
        decision = decide_home_dispatch(_home_inputs(indoor_temp_f=79.0, comfort_f=76.0, setback_f=82.0))
        assert decision.notify is HomeNotifyOutcome.SEND

    def test_indoor_temp_none_skips_near_comfort_check(self) -> None:
        decision = decide_home_dispatch(_home_inputs(indoor_temp_f=None))
        assert decision.notify is HomeNotifyOutcome.SEND

    def test_debounce_active_suppresses(self) -> None:
        decision = decide_home_dispatch(
            _home_inputs(
                indoor_temp_f=90.0,  # far from both comfort and setback -> no near-comfort suppress
                debounce_seconds=3600.0,
                seconds_since_last_notified=100.0,
            )
        )
        assert decision.notify is HomeNotifyOutcome.SUPPRESSED_DEBOUNCE

    def test_debounce_elapsed_sends(self) -> None:
        decision = decide_home_dispatch(
            _home_inputs(indoor_temp_f=90.0, debounce_seconds=3600.0, seconds_since_last_notified=3601.0)
        )
        assert decision.notify is HomeNotifyOutcome.SEND

    def test_debounce_disabled_sends_immediately(self) -> None:
        decision = decide_home_dispatch(
            _home_inputs(indoor_temp_f=90.0, debounce_seconds=0.0, seconds_since_last_notified=1.0)
        )
        assert decision.notify is HomeNotifyOutcome.SEND

    def test_never_notified_before_sends(self) -> None:
        decision = decide_home_dispatch(
            _home_inputs(indoor_temp_f=90.0, debounce_seconds=3600.0, seconds_since_last_notified=None)
        )
        assert decision.notify is HomeNotifyOutcome.SEND
