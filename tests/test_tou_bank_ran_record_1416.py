"""Issue #1416: a TOU bank write is a "ran" record for its side, even when the mode does not change.

Occupant framing: on a thermostat that does not report ``hvac_action``, a home pre-cooled for a high-cost period
used to get heat 68 F at the bank's end (the furnace armed on a hot afternoon; the mirror: the AC armed after a
winter pre-heat). The bank was written in the mode the thermostat was already in, so no mode change was recorded,
and without ``hvac_action`` nothing said the cool side ran: the comfort-edge settling guard (#1073) had nothing to
hold. Every accepted bank write now records its side (``AutomationEngine._note_deliberate_command``), and the guard
reads it through ``_minutes_since_family_ran``. See ``docs/comfort-edge-selection-spec.md`` (R3).

Real ``AutomationEngine`` and the fake thermostat of ``test_comfort_settling_guard`` (it reports no
``hvac_action``); the bank goes through the real ``apply_tou_precondition`` /
``apply_tou_away_vacation_precondition`` and the assertions are on the mode actually written.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from custom_components.climate_advisor.automation import HvacCommandResult
from custom_components.climate_advisor.const import OCCUPANCY_AWAY, OCCUPANCY_VACATION
from tests.test_comfort_settling_guard import (
    _AUTOMATION,
    _T0,
    _apply,
    _classification,
    _Clock,
    _make_engine,
    _mode,
    _mode_writes,
    _run,
)

# (side, day, bank target, indoor at the bank's end: inside the OTHER side's 2 F arming margin)
_SIDES = [("cool", "hot", 66.0, 68.5), ("heat", "cold", 76.0, 73.5)]


def _home_bank(engine, clock: _Clock, day: str, target: float) -> None:
    c = _classification(day)
    engine._current_classification = c
    with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
        _run(engine.apply_tou_precondition(c, target, "peak1"))


def _away_bank(engine, clock: _Clock, day: str, target: float, mode: str, occupancy: str) -> None:
    c = _classification(day)
    engine._current_classification = c
    engine._occupancy_mode = occupancy
    with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
        _run(engine.apply_tou_away_vacation_precondition(c, target, "peak1", mode))


class TestHomeBankCountsAsRan:
    @pytest.mark.parametrize(("side", "day", "target", "indoor"), _SIDES)
    def test_same_mode_bank_holds_its_side_at_the_bank_end(self, side, day, target, indoor):
        engine = _make_engine(indoor=indoor, mode=side)  # the bank's mode is already armed
        clock = _Clock()
        _home_bank(engine, clock, day, target)
        assert engine.comfort_mode_changes == [], "a same-mode bank is not a mode change (dwell/report unchanged)"
        clock.advance(minutes=45)
        _apply(engine, clock, day, indoor)
        other = "heat" if side == "cool" else "cool"
        assert other not in _mode_writes(engine), f"the {other} side must not be armed right after a {side} bank"
        assert _mode(engine) == side
        hold = engine._last_comfort_hold
        assert hold is not None and hold["zone"] == "margin"

    @pytest.mark.parametrize(("side", "day", "target", "indoor"), _SIDES)
    def test_control_without_the_bank_the_nearer_edge_is_armed(self, side, day, target, indoor):
        """The same scene with no bank write: nothing ran, so the margin arms the other side (unchanged)."""
        engine = _make_engine(indoor=indoor, mode=side)
        clock = _Clock()
        clock.advance(minutes=45)
        _apply(engine, clock, day, indoor)
        assert _mode(engine) == ("heat" if side == "cool" else "cool")
        assert engine._last_comfort_hold is None

    @pytest.mark.parametrize(("side", "day", "target", "indoor"), _SIDES)
    def test_record_expires_with_the_recency_window(self, side, day, target, indoor):
        engine = _make_engine(indoor=indoor, mode=side)
        clock = _Clock()
        _home_bank(engine, clock, day, target)
        window = float(engine.config.get("comfort_family_recency_window_min", 120.0))
        clock.advance(minutes=window + 1)
        _apply(engine, clock, day, indoor)
        assert _mode(engine) == ("heat" if side == "cool" else "cool"), "past the window the bank no longer holds"
        assert engine._last_comfort_hold is None

    def test_record_is_per_side_and_newest_bank_write_wins(self):
        engine = _make_engine(indoor=70.0, mode="cool")
        clock = _Clock()
        _home_bank(engine, clock, "hot", 66.0)
        clock.advance(minutes=30)
        _home_bank(engine, clock, "hot", 66.0)  # the 30-minute cycle re-asserts the bank
        later = clock.now + timedelta(minutes=10)
        assert engine.minutes_since_deliberate_command("cool", later) == pytest.approx(10.0)
        assert engine.minutes_since_deliberate_command("heating", later) is None
        assert engine._minutes_since_family_ran("cooling", later) == pytest.approx(10.0)


class TestAwayVacationBankCountsAsRan:
    @pytest.mark.parametrize("occupancy", [OCCUPANCY_AWAY, OCCUPANCY_VACATION])
    @pytest.mark.parametrize(("side", "day", "target"), [("cool", "hot", 77.0), ("heat", "cold", 63.0)])
    def test_away_bank_write_records_its_side(self, occupancy, side, day, target):
        engine = _make_engine(indoor=72.0, mode=side)
        clock = _Clock()
        _away_bank(engine, clock, day, target, side, occupancy)
        assert engine.comfort_mode_changes == []
        later = clock.now + timedelta(minutes=20)
        family = "cooling" if side == "cool" else "heating"
        other = "heating" if side == "cool" else "cooling"
        assert engine._minutes_since_family_ran(family, later) == pytest.approx(20.0)
        assert engine._minutes_since_family_ran(other, later) is None


class TestWhatIsNotARecord:
    @pytest.mark.parametrize(
        "result",
        [
            None,
            HvacCommandResult.FAILED,
            HvacCommandResult.DRY_RUN,
            HvacCommandResult.WHF_BLOCKED,
            HvacCommandResult.SUPERSEDED,
        ],
    )
    def test_unsent_bank_write_records_nothing(self, result):
        engine = _make_engine(indoor=70.0, mode="cool")
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_T0):
            engine._note_deliberate_command("cool", "tou", result=result)
        assert engine.minutes_since_deliberate_command("cool", _T0 + timedelta(minutes=5)) is None

    @pytest.mark.parametrize("result", [HvacCommandResult.EXECUTED, HvacCommandResult.UNCONFIRMED])
    def test_sent_bank_write_records(self, result):
        engine = _make_engine(indoor=70.0, mode="cool")
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_T0):
            engine._note_deliberate_command("heat", "tou", result=result)
        assert engine.minutes_since_deliberate_command("heat", _T0 + timedelta(minutes=5)) == pytest.approx(5.0)

    @pytest.mark.parametrize("mode", ["off", "heat_cool", "fan_only", None])
    def test_non_heat_cool_mode_records_nothing(self, mode):
        engine = _make_engine(indoor=70.0, mode="cool")
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_T0):
            engine._note_deliberate_command(mode, "tou", result=HvacCommandResult.EXECUTED)
        assert engine._deliberate_commands == {}

    def test_occupancy_redirect_writes_no_bank_and_records_nothing(self):
        """Home bank called while Away: ``_set_temperature_for_mode`` redirects to the setback (returns None)."""
        engine = _make_engine(indoor=70.0, mode="cool")
        engine._occupancy_mode = OCCUPANCY_AWAY
        clock = _Clock()
        _home_bank(engine, clock, "hot", 66.0)
        assert engine.minutes_since_deliberate_command("cool", clock.now) is None


class TestPersistence:
    def test_record_survives_a_restart(self):
        engine = _make_engine(indoor=70.0, mode="cool")
        clock = _Clock()
        _home_bank(engine, clock, "hot", 66.0)
        saved = engine.get_serializable_state()
        assert set(saved["deliberate_commands"]) == {"cool"}
        assert saved["deliberate_commands"]["cool"]["reason"] == "tou"
        fresh = _make_engine(indoor=70.0, mode="cool")
        fresh.restore_state(saved)
        later = clock.now + timedelta(minutes=15)
        assert fresh.minutes_since_deliberate_command("cool", later) == pytest.approx(15.0)

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "x",
            [1],
            {"cool": "not-a-date"},
            {"cool": {"ts": "not-a-date", "reason": "tou"}},
            {"auto": {"ts": _T0.isoformat(), "reason": "tou"}},
            {"heat": {"ts": _T0.isoformat(), "reason": "selector"}},
            {"heat": {"ts": _T0.isoformat(), "reason": "pre_cool"}},  # pre-cool stands for cool only
            {"heat": 5},
        ],
    )
    def test_garbage_is_dropped(self, raw):
        engine = _make_engine(indoor=70.0, mode="cool")
        engine.restore_state({"deliberate_commands": raw})
        assert engine._deliberate_commands == {}
