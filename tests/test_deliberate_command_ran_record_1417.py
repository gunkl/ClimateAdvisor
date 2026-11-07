"""Issue #1417 (mirror of #1416): every deliberate command is a "ran" record for its side, even in the unchanged mode.

Occupant framing: on a thermostat that does not report ``hvac_action``, an overnight pre-cool written while cool was
already armed left no record that the cool side ran, so on a hot morning the wake-up armed the furnace (heat at the
floor, 0.5 F under the house temperature). The same gap existed for the ceiling guard's cool write. Both now stamp
their side through ``AutomationEngine._note_deliberate_command`` (the one record #1416 introduced for TOU banks),
which the settling guard reads through ``_minutes_since_family_ran``. See ``docs/comfort-edge-selection-spec.md`` (R3).

Real ``AutomationEngine`` with the stub thermostats of the pre-cool and ceiling-guard tests (neither reports
``hvac_action``); the commands go through the real ``handle_pre_cool`` / ``apply_classification``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from custom_components.climate_advisor.automation import HvacCommandResult
from tests.test_ceiling_guard_state_skip_r6 import _NOW as _CG_NOW
from tests.test_ceiling_guard_state_skip_r6 import _run as _run_ceiling_guard
from tests.test_pre_cool import _make_classification
from tests.test_pre_cool_overnight_1371 import _pre_cooled_engine

_WAKE = datetime(2026, 7, 2, 6, 30, tzinfo=UTC)


def _mode_writes(engine) -> list[str]:
    return [
        c.args[2]["hvac_mode"]
        for c in engine.hass.services.async_call.call_args_list
        if c.args[0] == "climate" and "hvac_mode" in c.args[2]
    ]


def _wake_at(engine, box, indoor: float, at: datetime = _WAKE) -> None:
    """Wake-up on a hot day with the house at ``indoor`` (inside the 70 F floor's 2 F heat margin)."""
    box[0] = at
    state = engine.hass.states.get.return_value
    state.attributes = {**state.attributes, "current_temperature": indoor}
    engine._current_classification = _make_classification(day_type="hot", hvac_mode="cool")
    engine.hass.services.async_call.reset_mock()
    asyncio.run(engine.handle_morning_wakeup())


class TestPreCool:
    def test_same_mode_pre_cool_records_cool(self, monkeypatch):
        engine, _c, box, _target = _pre_cooled_engine(monkeypatch)  # cool already armed
        assert not [r for r in engine._comfort_mode_changes if r.get("reason") == "pre_cool"], "no mode change"
        assert engine._deliberate_commands["cool"]["reason"] == "pre_cool"
        box[0] = box[0] + timedelta(minutes=30)
        assert engine.minutes_since_deliberate_command("cool", box[0]) == pytest.approx(30.0, abs=0.5)
        assert engine._minutes_since_family_ran("cooling", box[0]) == pytest.approx(30.0, abs=0.5)

    def test_the_held_pre_cool_cycle_refreshes_the_record(self, monkeypatch):
        engine, c, box, _target = _pre_cooled_engine(monkeypatch)
        box[0] = datetime(2026, 7, 2, 6, 0, tzinfo=UTC)
        asyncio.run(engine.apply_classification(c))  # the 30-minute cycle keeps (re-sends) the pre-cool
        assert engine.minutes_since_deliberate_command("cool", _WAKE) == pytest.approx(30.0, abs=0.5)

    def test_wake_up_after_a_same_mode_pre_cool_keeps_cool(self, monkeypatch):
        engine, c, box, _target = _pre_cooled_engine(monkeypatch)
        box[0] = datetime(2026, 7, 2, 6, 0, tzinfo=UTC)
        asyncio.run(engine.apply_classification(c))
        _wake_at(engine, box, 70.5)
        assert "heat" not in _mode_writes(engine), "the furnace must not be armed on a hot morning after a pre-cool"
        assert engine._last_comfort_hold is not None and engine._last_comfort_hold["zone"] == "margin"

    def test_control_record_older_than_the_window_releases(self, monkeypatch):
        """02:30 pre-cool, no re-send, wake at 06:30 (240 min > the 120 min window): heat is armed as before."""
        engine, _c, box, _target = _pre_cooled_engine(monkeypatch)
        _wake_at(engine, box, 70.5)
        assert "heat" in _mode_writes(engine)
        assert engine._last_comfort_hold is None

    def test_a_heat_write_inside_a_pre_cool_pass_is_not_the_pre_cool(self, monkeypatch):
        engine, _c, _box, _target = _pre_cooled_engine(monkeypatch)
        engine._deliberate_commands = {}
        engine._note_deliberate_command("heat", "pre_cool", result=HvacCommandResult.EXECUTED)
        assert engine._deliberate_commands == {}


class TestCeilingGuard:
    def test_same_mode_guard_write_records_cool(self):
        """Thermostat already in cool but above the 74 F ceiling (e.g. its state still lags the band write)."""
        engine, fired = _run_ceiling_guard(mode="cool", setpoint=76.0)
        assert len(fired) == 1 and fired[0]["old_hvac_mode"] == "cool"
        assert engine._deliberate_commands["cool"]["reason"] == "ode_guard"
        assert engine.minutes_since_deliberate_command("cool", _CG_NOW + timedelta(minutes=10)) == pytest.approx(10.0)

    def test_x1_silent_guard_records_nothing(self):
        engine, fired = _run_ceiling_guard(mode="cool", setpoint=74.0)
        assert fired == []
        assert "cool" not in engine._deliberate_commands, "the selector's band write is not deliberate"

    def test_observe_only_guard_records_nothing(self):
        engine, _fired = _run_ceiling_guard(mode="off", setpoint=None, dry_run=True)
        assert engine._deliberate_commands == {}


class TestOccupantSetpointChange:
    def test_setpoint_only_override_is_not_a_record(self, monkeypatch):
        """Deliberately NOT covered: a setpoint-only change can ask for less heating/cooling, so it counts only when
        the equipment is seen running (the #1073 pin
        test_setpoint_only_override_without_equipment_run_does_not_hold)."""
        engine, _c, box, _target = _pre_cooled_engine(monkeypatch)
        engine._deliberate_commands = {}
        with patch("custom_components.climate_advisor.automation.dt_util.now", return_value=box[0]):
            engine.handle_manual_override(source="setpoint", old_mode="cool", new_mode="cool")
        assert engine._deliberate_commands == {}
        engine._note_deliberate_command("cool", "user", result=HvacCommandResult.EXECUTED)
        assert engine._deliberate_commands == {}


class TestOneRecord:
    def test_newest_deliberate_command_per_side_wins_whatever_its_reason(self, monkeypatch):
        engine, _c, box, _target = _pre_cooled_engine(monkeypatch)
        box[0] = box[0] + timedelta(minutes=20)
        engine._note_deliberate_command("cool", "tou", result=HvacCommandResult.UNCONFIRMED)
        assert engine._deliberate_commands["cool"]["reason"] == "tou"
        assert engine.minutes_since_deliberate_command("cool", box[0] + timedelta(minutes=5)) == pytest.approx(5.0)

    @pytest.mark.parametrize("reason", ["selector", "restore", "pause", "nat_vent", "startup", "other"])
    def test_non_deliberate_reasons_record_nothing(self, monkeypatch, reason):
        engine, _c, _box, _target = _pre_cooled_engine(monkeypatch)
        engine._deliberate_commands = {}
        engine._note_deliberate_command("cool", reason, result=HvacCommandResult.EXECUTED)
        assert engine._deliberate_commands == {}

    def test_pre_cool_record_survives_a_restart(self, monkeypatch):
        engine, _c, box, _target = _pre_cooled_engine(monkeypatch)
        saved = engine.get_serializable_state()
        assert saved["deliberate_commands"]["cool"]["reason"] == "pre_cool"
        engine._deliberate_commands = {}
        engine.restore_state(saved)
        assert engine.minutes_since_deliberate_command("cool", box[0] + timedelta(minutes=15)) == pytest.approx(
            15.0, abs=0.5
        )
