"""Tests for occupancy_priority.py (Issue #744) — pure guest > vacation > home/away
priority resolution, extracted from ClimateAdvisorCoordinator._compute_occupancy_mode().
"""

from __future__ import annotations

from custom_components.climate_advisor.const import (
    OCCUPANCY_AWAY,
    OCCUPANCY_GUEST,
    OCCUPANCY_HOME,
    OCCUPANCY_VACATION,
)
from custom_components.climate_advisor.occupancy_priority import (
    OccupancyPriorityInputs,
    decide_occupancy_priority,
)


def _inputs(**overrides) -> OccupancyPriorityInputs:
    base = dict(
        guest_configured=False,
        guest_on=False,
        vacation_configured=False,
        vacation_on=False,
        home_configured=False,
        home_on=False,
    )
    base.update(overrides)
    return OccupancyPriorityInputs(**base)


class TestNoTogglesConfigured:
    def test_defaults_to_home(self) -> None:
        assert decide_occupancy_priority(_inputs()) == OCCUPANCY_HOME


class TestHomeToggleOnly:
    def test_home_on_returns_home(self) -> None:
        assert decide_occupancy_priority(_inputs(home_configured=True, home_on=True)) == OCCUPANCY_HOME

    def test_home_off_returns_away(self) -> None:
        assert decide_occupancy_priority(_inputs(home_configured=True, home_on=False)) == OCCUPANCY_AWAY


class TestVacationPriority:
    def test_vacation_on_beats_home_on(self) -> None:
        result = decide_occupancy_priority(
            _inputs(vacation_configured=True, vacation_on=True, home_configured=True, home_on=True)
        )
        assert result == OCCUPANCY_VACATION

    def test_vacation_on_beats_home_off(self) -> None:
        result = decide_occupancy_priority(
            _inputs(vacation_configured=True, vacation_on=True, home_configured=True, home_on=False)
        )
        assert result == OCCUPANCY_VACATION

    def test_vacation_configured_but_off_falls_through_to_home(self) -> None:
        result = decide_occupancy_priority(
            _inputs(vacation_configured=True, vacation_on=False, home_configured=True, home_on=True)
        )
        assert result == OCCUPANCY_HOME


class TestGuestHighestPriority:
    def test_guest_on_beats_vacation_on(self) -> None:
        result = decide_occupancy_priority(
            _inputs(guest_configured=True, guest_on=True, vacation_configured=True, vacation_on=True)
        )
        assert result == OCCUPANCY_GUEST

    def test_guest_on_beats_home_away(self) -> None:
        result = decide_occupancy_priority(
            _inputs(guest_configured=True, guest_on=True, home_configured=True, home_on=False)
        )
        assert result == OCCUPANCY_GUEST

    def test_guest_configured_but_off_falls_through(self) -> None:
        result = decide_occupancy_priority(
            _inputs(guest_configured=True, guest_on=False, home_configured=True, home_on=True)
        )
        assert result == OCCUPANCY_HOME


class TestAllToggleCombinations:
    """Exhaustive sweep of the 3-toggle x on/off x configured/not space that matters:
    guest and vacation each independently configured-or-not, on-or-not; home always
    configured with both on/off, matching the realistic install shape (home toggle is
    the baseline; guest/vacation are optional add-ons)."""

    def test_only_guest_configured_and_on(self) -> None:
        assert decide_occupancy_priority(_inputs(guest_configured=True, guest_on=True)) == OCCUPANCY_GUEST

    def test_only_guest_configured_and_off(self) -> None:
        assert decide_occupancy_priority(_inputs(guest_configured=True, guest_on=False)) == OCCUPANCY_HOME

    def test_only_vacation_configured_and_on(self) -> None:
        assert decide_occupancy_priority(_inputs(vacation_configured=True, vacation_on=True)) == OCCUPANCY_VACATION

    def test_only_vacation_configured_and_off(self) -> None:
        assert decide_occupancy_priority(_inputs(vacation_configured=True, vacation_on=False)) == OCCUPANCY_HOME
