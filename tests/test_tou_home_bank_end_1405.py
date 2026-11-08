"""Issue #1405: a Home/Guest time-of-use bank ends at the high-cost period's start, not at the next cycle.

Occupant view: a home pre-cooled to 68 F for a 4:15 PM peak kept cooling to 68 F until the next 30-minute cycle
(up to 4:45 PM) wrote the comfort band; Away/Vacation already had a precise timer at the window start. The Home/Guest
bank now gets the same timer. These tests pin when it is armed, cancelled and what it writes (or leaves alone).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor import coordinator as cm
from custom_components.climate_advisor.const import OCCUPANCY_AWAY, OCCUPANCY_HOME
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.scheduler import TOUPhase, TOUPhaseResolution

NOW = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)
PEAK = datetime(2026, 7, 13, 16, 15, tzinfo=UTC)


def _res(phase=TOUPhase.PRECONDITIONING, start=PEAK, mode="cool", sid="peak1") -> TOUPhaseResolution:
    return TOUPhaseResolution(
        phase=phase, target=68.0, mode=mode, schedule_id=sid, schedule_start=start, precondition_start=NOW
    )


def _coord() -> ClimateAdvisorCoordinator:
    c = object.__new__(ClimateAdvisorCoordinator)
    c.hass = MagicMock()
    c._zone_scoped_sync = lambda fn: fn
    c._tou_home_end_timer_cancel = None
    c._tou_home_end_timer_key = None
    return c


class TestArming:
    def test_armed_for_the_period_start_while_a_home_bank_runs(self) -> None:
        coord = _coord()
        with patch.object(cm, "async_call_later", return_value=MagicMock()) as later:
            coord._sync_tou_home_end_timer(_res(), False, NOW)
        assert later.call_count == 1
        assert later.call_args.args[1] == (PEAK - NOW).total_seconds()
        assert coord._tou_home_end_timer_key == ("peak1", PEAK, "cool")

    def test_not_rearmed_for_the_same_bank(self) -> None:
        coord = _coord()
        with patch.object(cm, "async_call_later", return_value=MagicMock()) as later:
            coord._sync_tou_home_end_timer(_res(), False, NOW)
            coord._sync_tou_home_end_timer(_res(), False, NOW + timedelta(minutes=5))
        assert later.call_count == 1

    def test_away_vacation_and_no_bank_cancel_it(self) -> None:
        for res, away in ((_res(), True), (_res(phase=TOUPhase.NONE), False), (None, False)):
            coord = _coord()
            cancel = MagicMock()
            with patch.object(cm, "async_call_later", return_value=cancel):
                coord._sync_tou_home_end_timer(_res(), False, NOW)
                coord._sync_tou_home_end_timer(res, away, NOW)
            cancel.assert_called_once()
            assert coord._tou_home_end_timer_key is None

    def test_a_moved_period_re_arms(self) -> None:
        coord = _coord()
        first = MagicMock()
        with patch.object(cm, "async_call_later", side_effect=[first, MagicMock()]) as later:
            coord._sync_tou_home_end_timer(_res(), False, NOW)
            coord._sync_tou_home_end_timer(_res(start=PEAK + timedelta(minutes=15)), False, NOW)
        first.assert_called_once()
        assert later.call_count == 2

    def test_past_start_is_left_to_the_cycle(self) -> None:
        coord = _coord()
        with patch.object(cm, "async_call_later") as later:
            coord._sync_tou_home_end_timer(_res(), False, PEAK)
        later.assert_not_called()


def _fire_coord(*, holds: bool, occupancy: str = OCCUPANCY_HOME, coalescing: bool = False):
    coord = _coord()
    coord.automation_engine = MagicMock()
    coord.automation_engine.apply_classification = AsyncMock()
    coord._current_classification = MagicMock()
    coord._occupancy_mode = occupancy
    coord._tou_phase_resolution = _res()
    coord.config = {}
    coord._thermostat_holds_tou_bank = MagicMock(return_value=holds)
    coord._suppress_during_startup_coalescing = MagicMock(return_value=coalescing)

    def resolve():
        coord._tou_phase_resolution = _res(phase=TOUPhase.NONE)

    coord._resolve_tou_schedule_state = MagicMock(side_effect=resolve)
    coord.control_predicted_indoor = MagicMock(return_value=[])
    coord._get_indoor_temp = MagicMock(return_value=68.5)
    coord._push_tou_bank_card_status = MagicMock()
    return coord


class TestFire:
    def test_writes_the_band_when_the_thermostat_holds_the_bank(self) -> None:
        coord = _fire_coord(holds=True)
        asyncio.run(coord._fire_tou_home_bank_end(("peak1", PEAK, "cool")))
        coord._resolve_tou_schedule_state.assert_called_once()
        call = coord.automation_engine.apply_classification.await_args
        assert call.kwargs["tou_bank_owns_setpoint"] is False  # the bank is over: the band is written
        coord._push_tou_bank_card_status.assert_called_once()

    def test_a_deferred_or_replaced_bank_is_left_alone(self) -> None:
        coord = _fire_coord(holds=False)
        asyncio.run(coord._fire_tou_home_bank_end(("peak1", PEAK, "cool")))
        coord.automation_engine.apply_classification.assert_not_awaited()

    def test_another_occupancy_or_coalescing_or_a_different_bank_writes_nothing(self) -> None:
        for coord, key in (
            (_fire_coord(holds=True, occupancy=OCCUPANCY_AWAY), ("peak1", PEAK, "cool")),
            (_fire_coord(holds=True, coalescing=True), ("peak1", PEAK, "cool")),
            (_fire_coord(holds=True), ("other", PEAK, "cool")),
        ):
            asyncio.run(coord._fire_tou_home_bank_end(key))
            coord.automation_engine.apply_classification.assert_not_awaited()


def test_cancel_is_idempotent_and_clears_the_key() -> None:
    coord = _coord()
    cancel = MagicMock()
    coord._tou_home_end_timer_cancel, coord._tou_home_end_timer_key = cancel, ("peak1", PEAK, "cool")
    coord._cancel_tou_home_end_timer()
    coord._cancel_tou_home_end_timer()
    cancel.assert_called_once()
    assert coord._tou_home_end_timer_key is None
