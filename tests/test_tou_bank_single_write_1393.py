"""Issue #1393: while a time-of-use bank is open the thermostat is told the bank target once per cycle.

Occupant view: before the fix every 30-minute cycle inside the pre-cool/pre-heat window told the thermostat the comfort
edge and then the bank target in the same second (cool: 76 then 68; heat: 68 then 76), so the thermostat display
flicked to the wrong value and a cloud thermostat that dropped the second write left the house un-banked.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

_STUBS = Path(__file__).parent / "stubs"
if _STUBS.exists() and str(_STUBS) not in sys.path:
    sys.path.insert(0, str(_STUBS))

import pytest  # noqa: E402

from custom_components.climate_advisor.const import OCCUPANCY_AWAY, OCCUPANCY_HOME, OCCUPANCY_VACATION  # noqa: E402
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402
from custom_components.climate_advisor.scheduler import TOUPhase  # noqa: E402
from tests.test_tou_precondition import _make_cool_day_classification, _make_engine  # noqa: E402


def _writes(engine) -> list[tuple[str, float]]:
    return [
        (c.args[2]["hvac_mode"], c.args[2]["temperature"])
        for c in engine.hass.services.async_call.call_args_list
        if c.args[1] == "set_temperature"
    ]


def _cycle(engine, classification, *, bank_target: float | None, owns: bool) -> None:
    """The coordinator's regular cycle: apply_classification() then (when a bank is open) the TOU step."""
    asyncio.run(engine.apply_classification(classification, indoor_temp=72.0, tou_bank_owns_setpoint=owns))
    if bank_target is not None:
        asyncio.run(engine.apply_tou_precondition(classification, target=bank_target, schedule_id="peak1"))


def _cool():
    return _make_engine(indoor_temp=76.0, current_mode="cool"), _make_cool_day_classification()


def _heat():
    engine = _make_engine(indoor_temp=68.0, current_mode="heat")
    classification = _make_cool_day_classification()
    classification.day_type = "cold"
    classification.hvac_mode = "heat"
    return engine, classification


class TestBankCycleWritesOnce:
    def test_cool_during_bank_writes_only_the_bank_target(self):
        engine, c = _cool()
        _cycle(engine, c, bank_target=68.0, owns=True)
        assert _writes(engine) == [("cool", 68.0)]

    def test_heat_during_bank_writes_only_the_bank_target(self):
        engine, c = _heat()
        _cycle(engine, c, bank_target=76.0, owns=True)
        assert _writes(engine) == [("heat", 76.0)]

    def test_every_cycle_inside_the_window_reasserts_the_bank_target(self):
        engine, c = _cool()
        for _ in range(3):
            _cycle(engine, c, bank_target=68.0, owns=True)
        assert _writes(engine) == [("cool", 68.0)] * 3

    def test_window_start_cycle_cool_and_heat_write_the_bank_once(self):
        """The cycle that opens the bank is the first with owns=True: no comfort write ahead of it."""
        for make, target, mode in ((_cool, 68.0, "cool"), (_heat, 76.0, "heat")):
            engine, c = make()
            _cycle(engine, c, bank_target=target, owns=True)
            assert _writes(engine) == [(mode, target)]

    def test_before_the_window_the_comfort_band_is_written(self):
        """Phase NONE (owns=False): the comfort edge, exactly as before."""
        engine, c = _cool()
        _cycle(engine, c, bank_target=None, owns=False)
        assert _writes(engine) == [("cool", 76.0)]
        engine, c = _heat()
        _cycle(engine, c, bank_target=None, owns=False)
        assert _writes(engine) == [("heat", 68.0)]

    def test_window_end_restores_the_comfort_band_with_one_write(self):
        """After the bank (phase no longer PRECONDITIONING) the cycle restores the band: one write, no bank write."""
        for make, mode, edge in ((_cool, "cool", 76.0), (_heat, "heat", 68.0)):
            engine, c = make()
            _cycle(engine, c, bank_target=None, owns=False)
            assert _writes(engine) == [(mode, edge)]

    def test_cached_band_still_updated_when_the_bank_owns_the_setpoint(self):
        engine, c = _cool()
        _cycle(engine, c, bank_target=None, owns=True)
        assert engine._last_comfort_band is not None


class TestBankSkipHonouredOnlyWhenTheBankWillWrite:
    def test_manual_override_still_wins(self):
        engine, c = _cool()
        engine._manual_override_active = True
        engine._manual_override_mode = "heat"
        _cycle(engine, c, bank_target=68.0, owns=True)
        assert _writes(engine) == []

    def test_nat_vent_active_keeps_the_comfort_write(self):
        """The TOU step defers to nat-vent, so the skip must not leave the thermostat with no write at all."""
        engine, c = _cool()
        engine._natural_vent_active = True
        engine.config["aggressive_savings"] = False
        _cycle(engine, c, bank_target=68.0, owns=True)
        # TOU deferred (nat-vent); whatever apply_classification wrote is unchanged by the flag.
        engine2, c2 = _cool()
        engine2._natural_vent_active = True
        engine2.config["aggressive_savings"] = False
        _cycle(engine2, c2, bank_target=68.0, owns=False)
        assert _writes(engine) == _writes(engine2)

    def test_open_sensor_hold_keeps_the_pause_path(self):
        engine, c = _cool()
        engine._any_monitored_sensor_settled_open = MagicMock(return_value=True)
        engine._pause_for_door_window = AsyncMock()
        asyncio.run(engine.apply_classification(c, indoor_temp=72.0, tou_bank_owns_setpoint=True))
        engine._pause_for_door_window.assert_awaited()


class TestCoordinatorOwnership:
    def _coord(self, phase, occupancy=OCCUPANCY_HOME):
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord._tou_phase_resolution = SimpleNamespace(phase=phase) if phase is not None else None
        coord._occupancy_mode = occupancy
        return coord

    def test_preconditioning_home_owns(self):
        assert self._coord(TOUPhase.PRECONDITIONING)._tou_bank_owns_setpoint() is True

    @pytest.mark.parametrize("phase", [TOUPhase.NONE, None])
    def test_other_phases_do_not_own(self, phase):
        assert self._coord(phase)._tou_bank_owns_setpoint() is False

    @pytest.mark.parametrize("occ", [OCCUPANCY_AWAY, OCCUPANCY_VACATION])
    def test_away_vacation_bank_also_owns(self, occ):
        assert self._coord(TOUPhase.PRECONDITIONING, occ)._tou_bank_owns_setpoint() is True

    def test_no_attribute_is_false(self):
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord._occupancy_mode = OCCUPANCY_HOME
        assert coord._tou_bank_owns_setpoint() is False


# --- Issue #1395: the Away/Vacation mirror ------------------------------------------------------------------------
def _away(occupancy: str):
    engine = _make_engine(indoor_temp=76.0, current_mode="cool")
    engine.set_occupancy_mode(occupancy)
    return engine, _make_cool_day_classification()


class TestAwayVacationBankWritesOnce:
    """Before: away cool 82 then 73 (vacation 85 then 73) in the same second at every cycle inside the window."""

    @pytest.mark.parametrize("occupancy", ["away", "vacation"])
    def test_without_the_skip_the_setback_band_is_written_first(self, occupancy):
        engine, c = _away(occupancy)
        asyncio.run(engine.apply_classification(c, indoor_temp=76.0))
        asyncio.run(engine.apply_tou_away_vacation_precondition(c, 73.0, "peak1", "cool"))
        writes = _writes(engine)
        assert len(writes) == 2 and writes[1] == ("cool", 73.0) and writes[0][1] > 73.0

    @pytest.mark.parametrize("occupancy", ["away", "vacation"])
    def test_with_the_bank_owning_the_setpoint_only_the_bank_target_is_written(self, occupancy):
        engine, c = _away(occupancy)
        asyncio.run(engine.apply_classification(c, indoor_temp=76.0, tou_bank_owns_setpoint=True))
        asyncio.run(engine.apply_tou_away_vacation_precondition(c, 73.0, "peak1", "cool"))
        assert _writes(engine) == [("cool", 73.0)]

    def test_heat_mirror(self):
        engine = _make_engine(indoor_temp=62.0, current_mode="heat")
        engine.set_occupancy_mode("away")
        c = _make_cool_day_classification()
        c.hvac_mode, c.day_type = "heat", "cold"
        asyncio.run(engine.apply_classification(c, indoor_temp=62.0, tou_bank_owns_setpoint=True))
        asyncio.run(engine.apply_tou_away_vacation_precondition(c, 66.0, "peak1", "heat"))
        assert _writes(engine) == [("heat", 66.0)]

    def test_outside_the_bank_the_setback_band_is_still_written(self):
        engine, c = _away("away")
        asyncio.run(engine.apply_classification(c, indoor_temp=76.0, tou_bank_owns_setpoint=False))
        assert len(_writes(engine)) == 1

    def test_a_paused_door_keeps_the_setback_path(self):
        engine, c = _away("away")
        engine._paused_by_door = True
        engine.handle_occupancy_away = AsyncMock()
        asyncio.run(engine.apply_classification(c, indoor_temp=76.0, tou_bank_owns_setpoint=True))
        engine.handle_occupancy_away.assert_awaited_once()


# --- Issue #1393 wiring / #1395 call sites -------------------------------------------------------------------------
def _apply_classification_sites() -> dict[tuple[str, str], list[str]]:
    import ast  # noqa: PLC0415

    root = Path(__file__).parent.parent / "custom_components" / "climate_advisor"
    sites: dict[tuple[str, str], list[str]] = {}
    for name in ("coordinator.py", "api.py", "automation.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                for node in ast.walk(fn):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "apply_classification"
                    ):
                        kw = [k for k in node.keywords if k.arg == "tou_bank_owns_setpoint"]
                        sites[(name, fn.name)] = [ast.unparse(k.value) for k in kw]
    return sites


class TestCoordinatorWiring:
    def test_the_regular_cycle_passes_the_ownership_flag(self):
        """Fails if the coordinator stops telling apply_classification() the bank owns the setpoint."""
        sites = _apply_classification_sites()
        assert sites[("coordinator.py", "_async_update_data_impl")] == ["self._tou_bank_owns_setpoint()"]

    @pytest.mark.parametrize(
        ("file", "func"),
        [
            ("coordinator.py", "_do_startup_coalesce"),
            ("coordinator.py", "_async_send_briefing"),
            ("coordinator.py", "_async_reassert_setpoint_now"),
            ("api.py", "_apply_after_delay"),
            ("api.py", "_schedule_reclassify_after_cancel"),
            ("automation.py", "comfort_family_temperature_check"),
            ("automation.py", "_apply_current_scheduled_state"),
        ],
    )
    def test_other_call_sites_write_the_plain_band_and_rely_on_the_five_minute_check(self, file, func):
        """These passes are not followed by the TOU step, so they must not skip the band write (the thermostat would
        get no write at all); a comfort edge they leave mid-bank is re-banked by the 5-minute check (state-based)."""
        sites = _apply_classification_sites()
        assert sites[(file, func)] == []

    def test_every_call_site_is_accounted_for(self):
        """A new apply_classification() call site must be added to the two lists above on purpose."""
        assert len(_apply_classification_sites()) == 8


class TestBankWriteDoesNotScheduleAFiveMinuteLoop:
    """Issue #1395: each bank write scheduled a 5-minute follow-up that re-ran the cycle and re-sent the same bank
    target, for the whole bank (a write every 5 minutes)."""

    def test_home_bank_write_cancels_the_follow_up(self):
        engine, c = _cool()
        engine._cancel_classification_revisit = MagicMock()
        asyncio.run(engine.apply_tou_precondition(c, target=68.0, schedule_id="peak1"))
        engine._cancel_classification_revisit.assert_called()

    def test_away_bank_write_cancels_the_follow_up(self):
        engine, c = _away("away")
        engine._cancel_classification_revisit = MagicMock()
        asyncio.run(engine.apply_tou_away_vacation_precondition(c, 73.0, "peak1", "cool"))
        engine._cancel_classification_revisit.assert_called()


# --- Issue #1395: the Away/Vacation bank respects the same holds as the Home bank -----------------------------------
class TestAwayVacationBankRespectsHolds:
    """The shared gate answers DEFER_OCCUPANCY first for Away/Vacation and used to hide every hold below it."""

    @staticmethod
    def _bank(engine, c):
        asyncio.run(engine.apply_tou_away_vacation_precondition(c, 73.0, "peak1", "cool"))
        return _writes(engine)

    @pytest.mark.parametrize("occupancy", ["away", "vacation"])
    def test_no_hold_banks(self, occupancy):
        engine, c = _away(occupancy)
        assert self._bank(engine, c) == [("cool", 73.0)]

    @pytest.mark.parametrize("occupancy", ["away", "vacation"])
    @pytest.mark.parametrize(
        "hold",
        ["manual_override", "paused_by_door", "natural_vent", "whf"],
    )
    def test_each_hold_defers_the_bank(self, occupancy, hold):
        engine, c = _away(occupancy)
        if hold == "manual_override":
            engine._manual_override_active = True
        elif hold == "paused_by_door":
            engine._paused_by_door = True
            engine._sync_paused_by_door_with_live_sensors = AsyncMock()
        elif hold == "natural_vent":
            engine._natural_vent_active = True
        else:
            engine._whf_owns_hvac = MagicMock(return_value=True)
        assert self._bank(engine, c) == []

    @pytest.mark.parametrize("occupancy", ["away", "vacation"])
    def test_pending_override_confirm_defers_the_bank(self, occupancy):
        engine, c = _away(occupancy)
        engine._override_confirm_pending = True
        assert self._bank(engine, c) == []

    @pytest.mark.parametrize("hold", ["natural_vent", "whf"])
    def test_a_hold_keeps_the_setback_band_when_the_bank_defers(self, hold):
        """With nat-vent/WHF the bank writes nothing, so the cycle must not skip the setback band as well."""
        engine, c = _away("away")
        if hold == "natural_vent":
            engine._natural_vent_active = True
        else:
            engine._whf_owns_hvac = MagicMock(return_value=True)
        engine.handle_occupancy_away = AsyncMock()
        asyncio.run(engine.apply_classification(c, indoor_temp=76.0, tou_bank_owns_setpoint=True))
        engine.handle_occupancy_away.assert_awaited_once()
