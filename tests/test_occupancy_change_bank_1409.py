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
from custom_components.climate_advisor.ai_skills_context import (
    _render_occupancy_comfort_restored,
    _render_occupancy_setback,
)
from custom_components.climate_advisor.automation import HvacCommandResult, OccupancyBankWrite
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


async def _in_change(engine, coro):
    with engine.occupancy_change():
        await coro


def _engine(occupancy: str):
    engine = _make_engine(indoor_temp=76.0, current_mode="cool")
    engine.config.update({"setback_heat": 60.0, "setback_cool": 80.0})
    engine._current_classification = _make_cool_day_classification()
    engine.set_occupancy_mode(occupancy)
    return engine


class TestEngineHook:
    def test_leaving_with_a_bank_writes_no_setback(self) -> None:
        engine = _engine(OCCUPANCY_AWAY)
        engine._occupancy_bank_write = AsyncMock(
            return_value=OccupancyBankWrite(77.0, "cool", HvacCommandResult.EXECUTED)
        )
        asyncio.run(_in_change(engine, engine.handle_occupancy_away()))
        engine._occupancy_bank_write.assert_awaited_once()
        assert 80.0 not in _writes(engine)

    def test_bank_that_does_not_write_keeps_the_setback(self) -> None:
        engine = _engine(OCCUPANCY_AWAY)
        engine._occupancy_bank_write = AsyncMock(return_value=None)
        asyncio.run(_in_change(engine, engine.handle_occupancy_away()))
        assert 80.0 in _writes(engine)

    def test_other_callers_never_ask_the_bank(self) -> None:
        """The cycle, the period-start timer and the redirects call the same handler without the flag."""
        engine = _engine(OCCUPANCY_AWAY)
        engine._occupancy_bank_write = AsyncMock(
            return_value=OccupancyBankWrite(77.0, "cool", HvacCommandResult.EXECUTED)
        )
        asyncio.run(engine.handle_occupancy_away())
        engine._occupancy_bank_write.assert_not_awaited()
        assert 80.0 in _writes(engine)

    def test_a_bank_fault_falls_back_to_the_band(self) -> None:
        engine = _engine(OCCUPANCY_AWAY)
        engine._occupancy_bank_write = AsyncMock(side_effect=RuntimeError("boom"))
        asyncio.run(_in_change(engine, engine.handle_occupancy_away()))
        assert 80.0 in _writes(engine)

    def test_coming_home_with_a_bank_writes_no_comfort_restore(self) -> None:
        engine = _engine(OCCUPANCY_AWAY)
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._occupancy_bank_write = AsyncMock(
            return_value=OccupancyBankWrite(77.0, "cool", HvacCommandResult.EXECUTED)
        )
        asyncio.run(_in_change(engine, engine.handle_occupancy_home()))
        engine._occupancy_bank_write.assert_awaited_once()
        assert _writes(engine) == []

    def test_coming_home_without_a_bank_restores_comfort(self) -> None:
        """Control for the test above: the same change with no bank writes the comfort restore."""
        engine = _engine(OCCUPANCY_AWAY)
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        engine._occupancy_bank_write = AsyncMock(return_value=None)
        asyncio.run(_in_change(engine, engine.handle_occupancy_home()))
        assert _writes(engine) != []


def _coord(
    *, bank: bool = True, result: HvacCommandResult | None = HvacCommandResult.EXECUTED, coalescing: bool = False
) -> ClimateAdvisorCoordinator:
    """``result`` is what the bank write's record says (None = no write happened)."""
    coord = object.__new__(ClimateAdvisorCoordinator)
    ae = MagicMock()
    ae._hvac_write_record = MagicMock(result=HvacCommandResult.EXECUTED)
    coord.automation_engine = ae
    coord._current_classification = MagicMock()
    coord._startup_coalesce_active = coalescing
    coord._resolve_tou_schedule_state = MagicMock()
    coord._tou_bank_in_force = MagicMock(return_value=MagicMock(target=77.0, mode="cool") if bank else None)

    async def apply():
        if result is not None:
            ae._hvac_write_record = MagicMock(result=result)

    coord._apply_tou_schedule = AsyncMock(side_effect=apply)
    return coord


def _write(coord):
    with patch.object(cm.dt_util, "now", return_value=NOW):
        return asyncio.run(coord._occupancy_bank_write())


class TestCoordinatorWriter:
    def test_writes_the_new_bank_and_reports_what_was_sent(self) -> None:
        coord = _coord()
        assert _write(coord) == OccupancyBankWrite(77.0, "cool", HvacCommandResult.EXECUTED)
        coord._resolve_tou_schedule_state.assert_called_once()  # resolved for the NEW occupancy
        coord._apply_tou_schedule.assert_awaited_once()

    def test_an_unconfirmed_write_counts(self) -> None:
        assert _write(_coord(result=HvacCommandResult.UNCONFIRMED)) is not None

    def test_a_failed_or_rejected_write_falls_back_to_the_band(self) -> None:
        """Reviewer finding: a failed/rejected write also changes the write record. It must not count, or the band
        fallback is skipped and the house keeps the OLD occupancy's bank until the 5-minute check."""
        for bad in (
            r for r in HvacCommandResult if r not in (HvacCommandResult.EXECUTED, HvacCommandResult.UNCONFIRMED)
        ):
            assert _write(_coord(result=bad)) is None, bad

    def test_no_bank_window_or_a_deferring_gate_writes_nothing(self) -> None:
        """_tou_bank_in_force is None outside the window AND when tou_bank_gates_pass() says a gate defers it."""
        coord = _coord(bank=False)
        assert _write(coord) is None
        coord._apply_tou_schedule.assert_not_awaited()

    def test_coalescing_writes_nothing(self) -> None:
        coord = _coord(coalescing=True)
        assert asyncio.run(coord._occupancy_bank_write()) is None
        coord._resolve_tou_schedule_state.assert_not_called()

    def test_a_bank_write_that_did_not_happen_is_none(self) -> None:
        assert _write(_coord(result=None)) is None


class TestEntry:
    def test_the_change_is_scoped_to_the_handler_task(self) -> None:
        """The flag is a context variable: set inside the handler, gone after it, and a CONCURRENT coroutine (a cycle
        reapplying the setback while the handler awaits) never sees it."""
        engine = _engine(OCCUPANCY_AWAY)
        engine._occupancy_bank_write = AsyncMock(return_value=OccupancyBankWrite(77.0, "cool"))
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord.automation_engine = engine
        coord._occupancy_bank_write = AsyncMock(return_value=OccupancyBankWrite(77.0, "cool"))
        seen: dict[str, object] = {}

        async def concurrent():
            seen["other"] = await engine._bank_takes_occupancy_write("away")

        async def handler():
            other = asyncio.get_running_loop().create_task(concurrent())
            await asyncio.sleep(0)
            seen["inside"] = await engine._bank_takes_occupancy_write("away")
            await other

        async def run():
            await coord._run_occupancy_handler(OCCUPANCY_AWAY)
            seen["after"] = await engine._bank_takes_occupancy_write("away")

        engine.handle_occupancy_away = handler
        asyncio.run(run())
        assert seen["inside"] is not None
        assert seen["after"] is None
        # a task created inside copies the context: only tasks spawned BY the handler inherit it
        assert seen["other"] is not None

    def test_a_separate_task_never_sees_the_change(self) -> None:
        engine = _engine(OCCUPANCY_AWAY)
        engine._occupancy_bank_write = AsyncMock(return_value=OccupancyBankWrite(77.0, "cool"))
        got: dict[str, object] = {}

        async def main():
            gate = asyncio.Event()

            async def other():  # started BEFORE the change, like a running cycle
                await gate.wait()
                got["other"] = await engine._bank_takes_occupancy_write("away")

            t = asyncio.get_running_loop().create_task(other())
            with engine.occupancy_change():
                gate.set()
                await asyncio.sleep(0)
                await t
                got["inside"] = await engine._bank_takes_occupancy_write("away")

        asyncio.run(main())
        assert got["other"] is None and got["inside"] is not None

    def test_guest_uses_the_home_handler(self) -> None:
        """Home <-> Guest: the restore inside a home bank window is replaced by the bank as well."""
        coord = object.__new__(ClimateAdvisorCoordinator)
        ae = MagicMock()
        ae.handle_occupancy_home = AsyncMock()
        coord.automation_engine = ae
        asyncio.run(coord._run_occupancy_handler(OCCUPANCY_GUEST))
        ae.handle_occupancy_home.assert_awaited_once()


def test_the_occupancy_bank_write_reads_control_not_the_display_hold() -> None:
    """Display reads control, never the reverse: the #1409 writer decides from _tou_bank_in_force() (the bank's own
    window and gates), not from current_hold() / _active_tou_bank() / tou_bank_holds_setpoint()."""
    import ast
    from pathlib import Path

    src = Path(cm.__file__).read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_occupancy_bank_write"
    )
    names = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    assert "_tou_bank_in_force" in names
    assert not names & {"current_hold", "current_hold_payload", "_active_tou_bank", "tou_bank_holds_setpoint"}


class TestRecords:
    def test_leaving_records_the_bank_that_was_sent(self) -> None:
        engine = _engine(OCCUPANCY_AWAY)
        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda t, p: events.append((t, p))
        engine._occupancy_bank_write = AsyncMock(
            return_value=OccupancyBankWrite(77.0, "cool", HvacCommandResult.EXECUTED)
        )
        asyncio.run(_in_change(engine, engine.handle_occupancy_away()))
        rec = next(p for t, p in events if t == "occupancy_setback")
        assert rec["tou_bank"] is True and rec["target_f"] == 77.0 and rec["hvac_mode"] == "cool"
        assert "time-of-use bank owns the setpoint" in rec["note"]
        label, settings = _render_occupancy_setback(rec, "fahrenheit")
        assert settings.startswith("sent cool 77°F (time-of-use bank owns the setpoint")

    def test_coming_home_records_the_bank_that_was_sent(self) -> None:
        engine = _engine(OCCUPANCY_AWAY)
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda t, p: events.append((t, p))
        engine._occupancy_bank_write = AsyncMock(
            return_value=OccupancyBankWrite(76.0, "heat", HvacCommandResult.EXECUTED)
        )
        asyncio.run(_in_change(engine, engine.handle_occupancy_home()))
        rec = next(p for t, p in events if t == "occupancy_comfort_restored")
        assert (rec["mode"], rec["target_f"], rec["tou_bank"]) == ("heat", 76.0, True)
        _label, settings = _render_occupancy_comfort_restored(rec, "fahrenheit")
        assert settings == "setpoint: 76°F (time-of-use bank owns the setpoint; comfort band after it)"

    def test_without_a_bank_the_records_are_unchanged(self) -> None:
        engine = _engine(OCCUPANCY_AWAY)
        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda t, p: events.append((t, p))
        engine._occupancy_bank_write = AsyncMock(return_value=None)
        asyncio.run(_in_change(engine, engine.handle_occupancy_away()))
        rec = next(p for t, p in events if t == "occupancy_setback")
        assert "tou_bank" not in rec and (rec["floor"], rec["ceiling"]) == (60.0, 80.0)
