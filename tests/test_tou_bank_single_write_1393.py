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
    def test_away_vacation_not_covered(self, occ):
        assert self._coord(TOUPhase.PRECONDITIONING, occ)._tou_bank_owns_setpoint() is False

    def test_no_attribute_is_false(self):
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord._occupancy_mode = OCCUPANCY_HOME
        assert coord._tou_bank_owns_setpoint() is False
