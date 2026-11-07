"""Issue #1409: an occupancy change inside an open time-of-use bank window writes the new bank once.

Occupant view: leaving during a home pre-cool used to write the Away band (cool 80: the AC stops) and five minutes
later the Away bank (cool 77: it may restart), a short cycle; coming home during an Away pre-heat wrote heat 68 then
heat 76. Now the change writes the new occupancy's bank target directly, with the bank's own gates; when the bank
would defer, the band is written as before.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor import coordinator as cm
from custom_components.climate_advisor.const import OCCUPANCY_AWAY, OCCUPANCY_GUEST, OCCUPANCY_HOME
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from tests.test_tou_precondition import _make_cool_day_classification, _make_engine

NOW = datetime(2026, 7, 13, 15, 40, tzinfo=UTC)


def _writes(engine) -> list[float]:
    return [
        c.args[2]["temperature"]
        for c in engine.hass.services.async_call.call_args_list
        if c.args[1] == "set_temperature" and "temperature" in c.args[2]
    ]


def _engine(occupancy: str):
    engine = _make_engine(indoor_temp=76.0, current_mode="cool")
    engine.config.update({"setback_heat": 60.0, "setback_cool": 80.0})
    engine._current_classification = _make_cool_day_classification()
    engine.set_occupancy_mode(occupancy)
    return engine


class TestEngineHook:
    def test_leaving_with_a_bank_writes_no_setback(self) -> None:
        engine = _engine(OCCUPANCY_AWAY)
        engine._occupancy_bank_write = AsyncMock(return_value=True)
        engine._occupancy_change_in_progress = True
        asyncio.run(engine.handle_occupancy_away())
        engine._occupancy_bank_write.assert_awaited_once()
        assert 80.0 not in _writes(engine)

    def test_bank_that_does_not_write_keeps_the_setback(self) -> None:
        engine = _engine(OCCUPANCY_AWAY)
        engine._occupancy_bank_write = AsyncMock(return_value=False)
        engine._occupancy_change_in_progress = True
        asyncio.run(engine.handle_occupancy_away())
        assert 80.0 in _writes(engine)

    def test_other_callers_never_ask_the_bank(self) -> None:
        """The cycle, the period-start timer and the redirects call the same handler without the flag."""
        engine = _engine(OCCUPANCY_AWAY)
        engine._occupancy_bank_write = AsyncMock(return_value=True)
        asyncio.run(engine.handle_occupancy_away())
        engine._occupancy_bank_write.assert_not_awaited()
        assert 80.0 in _writes(engine)

    def test_a_bank_fault_falls_back_to_the_band(self) -> None:
        engine = _engine(OCCUPANCY_AWAY)
        engine._occupancy_bank_write = AsyncMock(side_effect=RuntimeError("boom"))
        engine._occupancy_change_in_progress = True
        asyncio.run(engine.handle_occupancy_away())
        assert 80.0 in _writes(engine)

    def test_coming_home_with_a_bank_writes_no_comfort_restore(self) -> None:
        engine = _engine(OCCUPANCY_AWAY)
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._occupancy_bank_write = AsyncMock(return_value=True)
        engine._occupancy_change_in_progress = True
        asyncio.run(engine.handle_occupancy_home())
        engine._occupancy_bank_write.assert_awaited_once()
        assert _writes(engine) == []

    def test_coming_home_without_a_bank_restores_comfort(self) -> None:
        """Control for the test above: the same change with no bank writes the comfort restore."""
        engine = _engine(OCCUPANCY_AWAY)
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._occupancy_bank_write = AsyncMock(return_value=False)
        engine._occupancy_change_in_progress = True
        asyncio.run(engine.handle_occupancy_home())
        assert _writes(engine) != []


def _coord(*, bank: bool = True, writes: bool = True, coalescing: bool = False) -> ClimateAdvisorCoordinator:
    coord = object.__new__(ClimateAdvisorCoordinator)
    ae = MagicMock()
    ae._hvac_write_record = object()
    coord.automation_engine = ae
    coord._current_classification = MagicMock()
    coord._startup_coalesce_active = coalescing
    coord._resolve_tou_schedule_state = MagicMock()
    coord._active_tou_bank = MagicMock(return_value=MagicMock() if bank else None)

    async def apply():
        if writes:
            ae._hvac_write_record = object()

    coord._apply_tou_schedule = AsyncMock(side_effect=apply)
    return coord


class TestCoordinatorWriter:
    def test_writes_the_new_bank_and_reports_it(self) -> None:
        coord = _coord()
        with patch.object(cm.dt_util, "now", return_value=NOW):
            assert asyncio.run(coord._occupancy_bank_write()) is True
        coord._resolve_tou_schedule_state.assert_called_once()  # resolved for the NEW occupancy
        coord._apply_tou_schedule.assert_awaited_once()

    def test_no_bank_window_or_a_deferring_gate_writes_nothing(self) -> None:
        """_active_tou_bank is None outside the window AND when tou_bank_holds_setpoint() says a gate defers it."""
        coord = _coord(bank=False)
        with patch.object(cm.dt_util, "now", return_value=NOW):
            assert asyncio.run(coord._occupancy_bank_write()) is False
        coord._apply_tou_schedule.assert_not_awaited()

    def test_coalescing_writes_nothing(self) -> None:
        coord = _coord(coalescing=True)
        assert asyncio.run(coord._occupancy_bank_write()) is False
        coord._resolve_tou_schedule_state.assert_not_called()

    def test_a_bank_write_that_did_not_happen_is_false(self) -> None:
        coord = _coord(writes=False)
        with patch.object(cm.dt_util, "now", return_value=NOW):
            assert asyncio.run(coord._occupancy_bank_write()) is False


class TestEntry:
    def test_the_flag_is_set_only_around_the_handler(self) -> None:
        coord = object.__new__(ClimateAdvisorCoordinator)
        ae = MagicMock()
        seen: list[bool] = []
        ae._occupancy_change_in_progress = False

        async def handler():
            seen.append(ae._occupancy_change_in_progress)

        ae.handle_occupancy_away = handler
        coord.automation_engine = ae
        asyncio.run(coord._run_occupancy_handler(OCCUPANCY_AWAY))
        assert seen == [True] and ae._occupancy_change_in_progress is False
        assert ae._occupancy_bank_write == coord._occupancy_bank_write

    def test_guest_uses_the_home_handler(self) -> None:
        """Home <-> Guest: the restore inside a home bank window is replaced by the bank as well."""
        coord = object.__new__(ClimateAdvisorCoordinator)
        ae = MagicMock()
        ae.handle_occupancy_home = AsyncMock()
        coord.automation_engine = ae
        asyncio.run(coord._run_occupancy_handler(OCCUPANCY_GUEST))
        ae.handle_occupancy_home.assert_awaited_once()
