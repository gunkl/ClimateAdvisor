"""Issue #1073: the settling guard (S1), user-action awareness (S2) and the setting's new meaning (S5).

Occupant framing: an AC that has just run (a TOU pre-cool, an overnight bank, the occupant's own
cooling) stops a degree or so past its setpoint. The house then reads 1 F under the comfort floor and,
without a settling rule, the furnace starts at once -- at peak price, minutes after the AC. The guard
HOLDS the current mode for a shallow breach (within ``comfort_deadband_*_f``) while the opposite side
ran inside ``comfort_family_recency_window_min``; a deeper breach, the day's natural side, and a lapsed
window all switch immediately. See ``docs/comfort-edge-selection-spec.md`` (R1-R9).

Real ``AutomationEngine`` + a fake thermostat that follows the engine's commands; the guard is observed
through the real selector (``apply_classification``, ``comfort_family_temperature_check``,
``_comfort_family_would_switch``) and the real leaf. Nothing here re-implements production logic.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import re
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.comfort_family_decision import (
    COMFORT_MIN_ARMING_MARGIN_F,
    ComfortFamilyInputs,
    ComfortFamilyOutcome,
    decide_comfort_family,
)
from custom_components.climate_advisor.comfort_family_fsm import (
    ComfortFamilyEvent,
    ComfortFamilyEventKind,
    ComfortFamilyFsmInputs,
    ComfortFamilyState,
)
from custom_components.climate_advisor.comfort_family_fsm import transition as fsm_transition

_AUTOMATION = "custom_components.climate_advisor.automation"
_T0 = datetime(2026, 10, 11, 14, 0, 0, tzinfo=UTC)
_BOTH = ["off", "heat", "cool"]
_FLOOR, _CEIL = 68.0, 74.0
_HVAC_MODE_FOR_DAY = {"hot": "cool", "warm": "off", "mild": "off", "cool": "off", "cold": "heat"}


def _consume_coroutine(coro):
    coro.close()


def _run(coro):
    return asyncio.run(coro)


class _Clock:
    def __init__(self, start: datetime = _T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


def _make_engine(*, indoor: float | None, mode: str, hvac_modes: list[str] | None = None, **cfg) -> AutomationEngine:
    hass = MagicMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()
    state = MagicMock()
    state.state = mode
    state.attributes = {
        "hvac_modes": list(hvac_modes or _BOTH),
        "supported_features": 1,
        "current_temperature": indoor,
    }
    hass.states.get.return_value = state

    async def _call(domain, service, data, **_kw):
        if domain != "climate":
            return None
        if service == "set_hvac_mode":
            state.state = data["hvac_mode"]
        elif service == "set_temperature":
            if "hvac_mode" in data:
                state.state = data["hvac_mode"]
            state.attributes["temperature"] = data["temperature"]
        return None

    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=_call)
    config = {
        "comfort_heat": _FLOOR,
        "comfort_cool": _CEIL,
        "setback_heat": 62.0,
        "setback_cool": 80.0,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
        **cfg,
    }
    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service=config["notify_service"],
        config=config,
    )
    engine._thermal_model = {}
    engine._test_state = state
    return engine


def _classification(day_type: str) -> DayClassification:
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.hvac_mode = _HVAC_MODE_FOR_DAY[day_type]
    obj.trend_direction = "stable"
    obj.trend_magnitude = 1.0
    obj.today_high = 95.0
    obj.today_low = 70.0
    obj.tomorrow_high = 95.0
    obj.tomorrow_low = 70.0
    obj.pre_condition = False
    obj.pre_condition_target = None
    obj.windows_recommended = False
    obj.window_open_time = None
    obj.window_close_time = None
    obj.setback_modifier = 0.0
    return obj


def _mode(engine: AutomationEngine) -> str:
    return engine._test_state.state


def _set_indoor(engine: AutomationEngine, value: float) -> None:
    engine._test_state.attributes["current_temperature"] = value


def _mode_writes(engine: AutomationEngine) -> list[str]:
    return [
        c.args[2]["hvac_mode"]
        for c in engine.hass.services.async_call.call_args_list
        if c.args[0] == "climate" and "hvac_mode" in c.args[2]
    ]


def _coordinator_class():
    return importlib.import_module("custom_components.climate_advisor.coordinator").ClimateAdvisorCoordinator


def _bare_coordinator(engine: AutomationEngine):
    cls = _coordinator_class()
    coord = object.__new__(cls)
    coord.automation_engine = engine
    coord._note_equipment_activity = types.MethodType(cls._note_equipment_activity, coord)
    return coord


def _stamp_ran(engine: AutomationEngine, direction: str, ago_min: float, now: datetime = _T0) -> None:
    """Record that the ``direction`` equipment ran ``ago_min`` ago through the REAL coordinator writer."""
    action = "cooling" if direction == "cool" else "heating"
    _bare_coordinator(engine)._note_equipment_activity(action, now=now - timedelta(minutes=ago_min))


def _apply(engine: AutomationEngine, clock: _Clock, day: str, indoor: float | None = None) -> None:
    c = _classification(day)
    engine._current_classification = c
    if indoor is not None:
        _set_indoor(engine, indoor)
    with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
        _run(engine.apply_classification(c))


def _reactive(engine: AutomationEngine, clock: _Clock, indoor: float) -> None:
    _set_indoor(engine, indoor)
    with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
        _run(engine.comfort_family_temperature_check(indoor, predicted_indoor=None))


def _scene(
    *,
    day: str,
    mode: str,
    indoor: float,
    ran: str | None,
    ran_min: float = 40.0,
    **cfg,
):
    """An engine in ``mode`` where the ``ran`` side's equipment last ran ``ran_min`` ago; selector applied."""
    engine = _make_engine(indoor=indoor, mode=mode, **cfg)
    clock = _Clock()
    if ran is not None:
        _stamp_ran(engine, ran, ran_min)
    _apply(engine, clock, day, indoor)
    return engine, clock


# ---------------------------------------------------------------------------
# R1 -- a deep breach always switches at once
# ---------------------------------------------------------------------------


class TestR1:
    @pytest.mark.parametrize(
        ("day", "mode", "indoor", "ran", "expected"),
        [
            # hot day: heat is against the grain, tolerance 5 -> depth 5.1 switches
            ("hot", "cool", 62.9, "cool", "heat"),
            # mild day tolerance 2 -> depth 2.1 switches (heat mirror)
            ("mild", "cool", 65.9, "cool", "heat"),
            # cold day: cool is against the grain, tolerance 5 -> depth 5.1 switches (cool mirror)
            ("cold", "heat", 79.1, "heat", "cool"),
            ("mild", "heat", 76.1, "heat", "cool"),
        ],
        ids=["heat-hot", "heat-mild", "cool-cold", "cool-mild"],
    )
    def test_R1_deep_breach_switches_immediately(self, day, mode, indoor, ran, expected):
        engine, _ = _scene(day=day, mode=mode, indoor=indoor, ran=ran, ran_min=5.0)
        assert _mode(engine) == expected
        assert engine._last_comfort_hold is None

    def test_R1_deep_breach_ignores_the_dwell_lock(self):
        engine, clock = _scene(day="hot", mode="cool", indoor=72.0, ran="cool", ran_min=5.0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), engine._mode_change_context("restore"):
            _run(engine._set_hvac_mode("heat", reason="restore"))
            _run(engine._set_hvac_mode("cool", reason="restore back"))  # arms the 600 s mode-change lock
        clock.advance(minutes=1)  # inside the lock
        _reactive(engine, clock, 62.0)  # depth 6 > the hot-day tolerance of 5
        assert _mode(engine) == "heat"


# ---------------------------------------------------------------------------
# R2 -- a shallow breach after the opposite side ran holds (both directions)
# ---------------------------------------------------------------------------


class TestR2:
    @pytest.mark.parametrize(
        ("day", "mode", "indoor", "ran"),
        [
            ("hot", "cool", 67.0, "cool"),  # depth 1
            ("hot", "cool", 63.0, "cool"),  # depth 5 == tolerance: still a hold
            ("mild", "cool", 66.0, "cool"),  # depth 2 == tolerance
            ("warm", "cool", 66.5, "cool"),
            ("cold", "heat", 75.0, "heat"),  # cool mirror
            ("cold", "heat", 79.0, "heat"),  # depth 5 == tolerance
            ("mild", "heat", 76.0, "heat"),
            ("cool", "heat", 75.5, "heat"),
        ],
    )
    def test_R2_shallow_breach_after_opposite_ran_holds(self, day, mode, indoor, ran):
        engine, _ = _scene(day=day, mode=mode, indoor=indoor, ran=ran, ran_min=40.0)
        assert _mode(engine) == mode, "the shallow overshoot must not flip the thermostat"
        assert _mode_writes(engine).count("heat" if mode == "cool" else "cool") == 0
        hold = engine._last_comfort_hold
        assert hold is not None
        assert hold["current_family"] == ("cooling" if mode == "cool" else "heating")
        assert hold["depth_f"] <= hold["tolerance_f"]

    def test_R2_window_lapsed_switches(self):
        engine, _ = _scene(day="hot", mode="cool", indoor=67.0, ran="cool", ran_min=121.0)
        assert _mode(engine) == "heat"

    def test_R2_window_lapsed_switches_cool_mirror(self):
        engine, _ = _scene(day="cold", mode="heat", indoor=75.0, ran="heat", ran_min=121.0)
        assert _mode(engine) == "cool"

    def test_R2_window_boundary_is_strict(self):
        engine, _ = _scene(day="mild", mode="cool", indoor=67.0, ran="cool", ran_min=119.0)
        assert _mode(engine) == "cool"
        engine2, _ = _scene(day="mild", mode="cool", indoor=67.0, ran="cool", ran_min=120.0)
        assert _mode(engine2) == "heat"

    def test_R2_no_known_run_means_no_hold(self):
        engine, _ = _scene(day="mild", mode="cool", indoor=67.0, ran=None)
        assert _mode(engine) == "heat"

    def test_R2_only_the_opposite_family_counts(self):
        """Heat ran recently but the thermostat is in cool: a floor breach is a switch TO heat -- the family
        being left (cool) did not run, so nothing is held."""
        engine, _ = _scene(day="mild", mode="cool", indoor=67.0, ran="heat", ran_min=10.0)
        assert _mode(engine) == "heat"

    def test_R2_configured_window_is_honoured(self):
        engine, _ = _scene(
            day="mild", mode="cool", indoor=67.0, ran="cool", ran_min=40.0, comfort_family_recency_window_min=30.0
        )
        assert _mode(engine) == "heat"  # 40 min > the 30 min window
        engine2, _ = _scene(
            day="mild", mode="cool", indoor=67.0, ran="cool", ran_min=40.0, comfort_family_recency_window_min=60.0
        )
        assert _mode(engine2) == "cool"

    def test_R2_configured_tolerance_is_honoured(self):
        engine, _ = _scene(day="mild", mode="cool", indoor=65.0, ran="cool", comfort_deadband_mild_f=4.0)
        assert _mode(engine) == "cool"  # depth 3 <= 4
        engine2, _ = _scene(day="mild", mode="cool", indoor=65.0, ran="cool", comfort_deadband_mild_f=2.0)
        assert _mode(engine2) == "heat"  # depth 3 > 2

    def test_R2_hold_ends_when_the_house_gets_deeper(self):
        engine, clock = _scene(day="hot", mode="cool", indoor=67.0, ran="cool")
        assert _mode(engine) == "cool"
        _reactive(engine, clock, 64.0)
        assert _mode(engine) == "cool"
        _reactive(engine, clock, 62.0)
        assert _mode(engine) == "heat"

    def test_R2_hold_ends_when_the_window_lapses(self):
        engine, clock = _scene(day="hot", mode="cool", indoor=67.0, ran="cool", ran_min=100.0)
        assert _mode(engine) == "cool"
        clock.advance(minutes=30)  # now 130 min since the run
        _reactive(engine, clock, 67.0)
        assert _mode(engine) == "heat"

    def test_R2_hold_applies_to_margin_zone_switches(self):
        """Issue #1073 (reviewer B1): the guard also covers the MARGIN zone. After the AC ran the house usually
        reads 68-70 first (inside the heat arming margin, not a breach); a later overshoot to 67 must not then
        run the furnace. Inverted from the earlier "guard is breach-only" rule."""
        engine, clock = _scene(day="mild", mode="cool", indoor=71.0, ran="cool", ran_min=10.0)
        assert _mode(engine) == "cool"
        _reactive(engine, clock, 69.5)  # inside the 2 F heat margin, not a breach
        assert _mode(engine) == "cool"
        _apply(engine, clock, "mild", 69.5)  # the full evaluation records the hold
        hold = engine._last_comfort_hold
        assert hold is not None and hold["zone"] == "margin" and hold["depth_f"] == 0.0
        _reactive(engine, clock, 67.0)  # now a breach, depth 1 <= 2: still held
        assert _mode(engine) == "cool"
        _apply(engine, clock, "mild", 67.0)
        assert engine._last_comfort_hold["zone"] == "breach"
        _reactive(engine, clock, 65.0)  # depth 3 > 2: R1
        assert _mode(engine) == "heat"

    def test_R2_unknown_current_family_with_nothing_run_adopts_without_a_hold(self):
        """Issue #1149: an unknown live family is held only when a family ran inside the window; with
        nothing run there is nothing to hold (the unknown-family hold itself: test_comfort_settling_unknown_family)."""
        engine, _ = _scene(day="mild", mode="off", indoor=67.0, ran=None)
        assert _mode(engine) == "heat"
        assert engine._last_comfort_hold is None

    # --- TOU bank end: real apply_tou_precondition + real selector, every occupancy band -----------------

    @staticmethod
    def _bank(direction: str, occupancy: str):
        day = "hot" if direction == "cool" else "cold"
        engine = _make_engine(indoor=71.0, mode="off")  # mid-gap of the 68-74 band
        clock = _Clock()
        c = _classification(day)
        engine._current_classification = c
        engine._occupancy_mode = occupancy
        bank_target = 64.0 if direction == "cool" else 76.0
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            engine.set_tou_precondition_window(clock.now + timedelta(hours=1), direction)
            if occupancy in ("away", "vacation"):
                _run(engine.apply_tou_away_vacation_precondition(c, bank_target, "s1", direction))
            else:
                _run(engine.apply_tou_precondition(c, bank_target, "s1"))
        assert _mode(engine) == direction
        clock.advance(minutes=65)  # the bank window ends
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            engine.set_tou_precondition_window(None)
            _run(engine.apply_classification(c))  # resolves this occupancy's band
        return engine, clock, c

    @pytest.mark.parametrize("occupancy", ["home", "guest", "away", "vacation"])
    @pytest.mark.parametrize("direction", ["cool", "heat"])
    def test_R2_tou_bank_end_overshoot_holds(self, direction, occupancy):
        engine, clock, c = self._bank(direction, occupancy)
        band = engine._last_comfort_band
        assert band is not None
        # one degree past the edge the bank pushed toward: floor for a cool bank, ceiling for a heat bank
        overshoot = band.floor - 1.0 if direction == "cool" else band.ceiling + 1.0
        _reactive(engine, clock, overshoot)
        _apply(engine, clock, "hot" if direction == "cool" else "cold", overshoot)
        assert _mode(engine) == direction, f"{occupancy}: the bank overshoot must be held, not flipped"
        assert engine._last_comfort_hold is not None
        # ...and a DEEP overshoot (beyond the tolerance) still switches at once
        deep = band.floor - 7.0 if direction == "cool" else band.ceiling + 7.0
        _apply(engine, clock, "hot" if direction == "cool" else "cold", deep)
        assert _mode(engine) == ("heat" if direction == "cool" else "cool")

    def test_R2_tou_bank_overshoot_switches_after_the_window_lapses(self):
        engine, clock, c = self._bank("cool", "home")
        clock.advance(minutes=130)
        _apply(engine, clock, "hot", engine._last_comfort_band.floor - 1.0)
        assert _mode(engine) == "heat"


# ---------------------------------------------------------------------------
# D2 = X -- the day's natural side is never delayed; mild has no natural side
# ---------------------------------------------------------------------------


class TestNaturalSide:
    @pytest.mark.parametrize(
        ("day", "mode", "indoor", "ran", "expected"),
        [
            ("hot", "heat", 74.5, "heat", "cool"),  # hot day, cooling is native: immediate
            ("warm", "heat", 75.0, "heat", "cool"),
            ("cold", "cool", 67.5, "cool", "heat"),  # cold day, heating is native: immediate
            ("cool", "cool", 67.0, "cool", "heat"),
        ],
    )
    def test_R2_natural_side_never_held(self, day, mode, indoor, ran, expected):
        engine, _ = _scene(day=day, mode=mode, indoor=indoor, ran=ran, ran_min=5.0)
        assert _mode(engine) == expected
        assert engine._last_comfort_hold is None

    def test_mild_day_has_no_natural_side_both_directions_hold_at_two(self):
        for mode, indoor, ran in (("cool", 66.0, "cool"), ("heat", 76.0, "heat")):
            engine, _ = _scene(day="mild", mode=mode, indoor=indoor, ran=ran, ran_min=10.0)
            assert _mode(engine) == mode
        for mode, indoor, ran, exp in (("cool", 65.9, "cool", "heat"), ("heat", 76.1, "heat", "cool")):
            engine, _ = _scene(day="mild", mode=mode, indoor=indoor, ran=ran, ran_min=10.0)
            assert _mode(engine) == exp

    def test_hot_day_against_grain_tolerance_is_five_warm_is_two(self):
        hot, _ = _scene(day="hot", mode="cool", indoor=63.5, ran="cool", ran_min=10.0)  # depth 4.5
        warm, _ = _scene(day="warm", mode="cool", indoor=63.5, ran="cool", ran_min=10.0)
        assert _mode(hot) == "cool"
        assert _mode(warm) == "heat"

    def test_R2_worked_examples_table(self):
        """Section 5 of the spec, row by row (band 68-74)."""
        # Hot/warm day, AC ran 40 min ago, house falls to 67 -> hold (heat is against the grain)
        for day in ("hot", "warm"):
            e, _ = _scene(day=day, mode="cool", indoor=67.0, ran="cool", ran_min=40.0)
            assert _mode(e) == "cool"
        # Warm day, furnace ran earlier, sun pushes the house to 76 -> cool now (native)
        e, _ = _scene(day="warm", mode="heat", indoor=76.0, ran="heat", ran_min=90.0)
        assert _mode(e) == "cool"
        # Cold day, furnace ran 30 min ago, overshoot to 75 -> hold
        e, _ = _scene(day="cold", mode="heat", indoor=75.0, ran="heat", ran_min=30.0)
        assert _mode(e) == "heat"
        # Cold morning, house 65, nothing ran for hours -> heat now
        e, _ = _scene(day="cold", mode="cool", indoor=65.0, ran=None)
        assert _mode(e) == "heat"
        # Mild day, AC ran 30 min ago, house 66 (depth 2 == tolerance) -> hold; 65.9 -> heat
        e, _ = _scene(day="mild", mode="cool", indoor=66.0, ran="cool", ran_min=30.0)
        assert _mode(e) == "cool"
        e, _ = _scene(day="mild", mode="cool", indoor=65.9, ran="cool", ran_min=30.0)
        assert _mode(e) == "heat"


# ---------------------------------------------------------------------------
# R3 -- one "ran" source: equipment, commands, user; survives restart
# ---------------------------------------------------------------------------


class TestR3:
    def test_R3_ran_source_includes_equipment_commands_and_user_and_survives_restart(self):
        # (a) equipment that ran under ANY commander counts
        engine = _make_engine(indoor=70.0, mode="cool")
        _stamp_ran(engine, "cool", 25.0)
        assert engine.minutes_since_equipment_ran("cool", _T0) == 25.0

        # (b) a commanded mode change counts through the mode-change clock (no equipment event at all)
        engine2 = _make_engine(indoor=70.0, mode="off")
        clock = _Clock()
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), engine2._mode_change_context("tou"):
            _run(engine2._set_hvac_mode("cool", reason="bank"))
        assert engine2.minutes_since_equipment_ran("cool", _T0) is None
        assert engine2.minutes_since_mode_set("cool", _T0 + timedelta(minutes=12)) == 12.0

        # (c) the occupant's own mode change counts
        engine3 = _make_engine(indoor=70.0, mode="heat")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), patch(f"{_AUTOMATION}.async_call_later"):
            engine3._test_state.state = "cool"
            engine3.handle_manual_override(old_mode="heat", new_mode="cool", classification_mode="heat")
        assert ("cool", "user") in [(c["to_mode"], c["reason"]) for c in engine3.comfort_mode_changes]
        assert engine3.minutes_since_mode_set("cool", _T0 + timedelta(minutes=3)) == 3.0

        # (d) a side running RIGHT NOW is "ran 0 minutes ago", even with a stale stamp
        engine4 = _make_engine(indoor=70.0, mode="cool")
        engine4._test_state.attributes["hvac_action"] = "cooling"
        engine4._last_hvac_cooling_active = (_T0 - timedelta(minutes=500)).isoformat()
        assert engine4.minutes_since_equipment_ran("cool", _T0) == 0.0
        assert engine4.minutes_since_equipment_ran("heat", _T0) is None

        # (e) restart: both clocks come back through the real serialize/restore path
        _stamp_ran(engine2, "heat", 10.0)
        saved = json.loads(json.dumps(engine2.get_serializable_state()))
        fresh = _make_engine(indoor=70.0, mode="cool")
        fresh.restore_state(saved)
        assert fresh.minutes_since_equipment_ran("heat", _T0) == 10.0
        assert fresh.minutes_since_mode_set("cool", _T0 + timedelta(minutes=12)) == 12.0

    def test_restart_with_a_ten_minute_old_run_still_holds(self):
        engine = _make_engine(indoor=70.0, mode="cool")
        _stamp_ran(engine, "cool", 10.0)
        saved = json.loads(json.dumps(engine.get_serializable_state()))

        fresh = _make_engine(indoor=67.0, mode="cool")  # thermostat still in cool after the restart
        fresh.restore_state(saved)
        _apply(fresh, _Clock(), "hot", 67.0)
        assert _mode(fresh) == "cool"
        assert fresh._last_comfort_hold is not None

    def test_without_the_restored_state_the_same_restart_flips(self):
        fresh = _make_engine(indoor=67.0, mode="cool")
        _apply(fresh, _Clock(), "hot", 67.0)
        assert _mode(fresh) == "heat"

    def test_currently_running_side_counts_as_ran_now(self):
        engine = _make_engine(indoor=67.0, mode="cool")
        engine._test_state.attributes["hvac_action"] = "cooling"  # compressor still on, no stamp at all
        _apply(engine, _Clock(), "hot", 67.0)
        assert _mode(engine) == "cool"
        assert engine._last_comfort_hold["opposite_ran_min_ago"] == 0.0


# ---------------------------------------------------------------------------
# R4 -- one mode-change clock fed by every writer, persisted
# ---------------------------------------------------------------------------


class TestR4:
    def test_R4_single_clock_fed_by_every_writer_and_persisted(self):
        engine = _make_engine(indoor=70.0, mode="off")
        clock = _Clock()
        seq = [("selector", "heat"), ("tou", "cool"), ("restore", "heat"), ("ode_guard", "cool"), ("pause", "heat")]
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            for reason, mode in seq:
                clock.advance(minutes=1)
                with engine._mode_change_context(reason):
                    _run(engine._set_hvac_mode(mode, reason=reason))
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), patch(f"{_AUTOMATION}.async_call_later"):
            clock.advance(minutes=1)
            engine._test_state.state = "cool"
            engine.handle_manual_override(old_mode="heat", new_mode="cool", classification_mode="heat")
        recorded = [(c["reason"], c["to_mode"]) for c in engine.comfort_mode_changes]
        assert recorded == [*seq, ("user", "cool")]
        assert engine._last_mode_change_time() == clock.now

        saved = json.loads(json.dumps(engine.get_serializable_state()))
        fresh = _make_engine(indoor=70.0, mode="cool")
        fresh.restore_state(saved)
        assert [(c["reason"], c["to_mode"]) for c in fresh.comfort_mode_changes] == recorded
        assert fresh._last_mode_change_time() == clock.now

    def test_clock_value_reaches_the_fsm_input(self):
        engine = _make_engine(indoor=70.0, mode="off")
        clock = _Clock()
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), engine._mode_change_context("tou"):
            _run(engine._set_hvac_mode("cool", reason="bank"))
        inputs, _ = engine._comfort_family_inputs(
            "cool", floor=_FLOOR, ceiling=_CEIL, now=clock.now, day_type="hot", indoor=70.0
        )
        assert inputs.last_mode_change_at == clock.now


# ---------------------------------------------------------------------------
# R5 / S2 -- user awareness
# ---------------------------------------------------------------------------


class TestR5UserActions:
    @staticmethod
    def _user_changes_mode_then_grace_expires(direction: str, day: str):
        """The occupant switches the thermostat to ``direction`` by hand; the override is confirmed and its
        grace period then expires through the real ``_on_grace_expired``."""
        start = "heat" if direction == "cool" else "cool"
        engine = _make_engine(indoor=71.0, mode=start)
        clock = _Clock()
        c = _classification(day)
        engine._current_classification = c
        engine.hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), patch(f"{_AUTOMATION}.async_call_later"):
            engine._test_state.state = direction  # the occupant's change
            engine.handle_manual_override(old_mode=start, new_mode=direction, classification_mode=c.hvac_mode)
            engine._confirm_override(direction)
            assert engine._manual_override_active
            clock.advance(minutes=30)
            engine._on_grace_expired("manual", 1800, False)
        assert not engine._manual_override_active
        return engine, clock, c

    @pytest.mark.parametrize(
        ("direction", "day", "overshoot", "deep"),
        [("cool", "mild", 67.0, 65.0), ("heat", "mild", 75.0, 77.0)],
        ids=["cooled-by-hand", "heated-by-hand"],
    )
    def test_R5_override_expiry_does_not_undo_user_action(self, direction, day, overshoot, deep):
        engine, clock, c = self._user_changes_mode_then_grace_expires(direction, day)
        assert "user" in [r["reason"] for r in engine.comfort_mode_changes]
        # the user's cooling dragged the house 1 F under the floor; after grace the system must not undo it
        _set_indoor(engine, overshoot)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine._apply_current_scheduled_state())
        assert _mode(engine) == direction
        assert engine._last_comfort_hold is not None
        # a DEEPER overshoot (> the mild 2 F tolerance) switches at once
        _set_indoor(engine, deep)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine._apply_current_scheduled_state())
        assert _mode(engine) == ("heat" if direction == "cool" else "cool")

    def test_R5_user_hold_lapses_with_the_window(self):
        engine, clock, c = self._user_changes_mode_then_grace_expires("cool", "mild")
        clock.advance(minutes=130)
        _set_indoor(engine, 67.0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine._apply_current_scheduled_state())
        assert _mode(engine) == "heat"

    @staticmethod
    def _setpoint_override_expired(clock: _Clock):
        """A setpoint-only override (mode unchanged) confirmed and expired through the real grace path."""
        engine = _make_engine(indoor=70.0, mode="cool")
        engine._current_classification = _classification("mild")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), patch(f"{_AUTOMATION}.async_call_later"):
            engine.handle_manual_override(source="setpoint", old_mode="cool", new_mode="cool")
            engine._confirm_override("cool", "setpoint")
            assert engine._manual_override_active
            clock.advance(minutes=30)
            engine._on_grace_expired("manual", 1800, False)
        assert not engine._manual_override_active
        assert engine.comfort_mode_changes == []  # a setpoint change is not a mode change
        return engine

    def test_setpoint_only_override_that_runs_the_equipment_counts_as_ran(self):
        """The occupant lowers the cool setpoint (mode unchanged); the thermostat then cools and the
        coordinator observes hvac_action=cooling. After the override ends the overshoot is held."""
        clock = _Clock()
        engine = self._setpoint_override_expired(clock)
        _stamp_ran(engine, "cool", 5.0, now=clock.now)
        assert engine.minutes_since_mode_set("cool", clock.now) is None
        assert engine.minutes_since_equipment_ran("cool", clock.now) == 5.0
        _set_indoor(engine, 67.0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine._apply_current_scheduled_state())
        assert _mode(engine) == "cool"
        assert engine._last_comfort_hold is not None

    def test_setpoint_only_override_without_equipment_run_does_not_hold(self):
        clock = _Clock()
        engine = self._setpoint_override_expired(clock)
        _set_indoor(engine, 67.0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine._apply_current_scheduled_state())
        assert _mode(engine) == "heat"  # nothing ran: honest negative control


# ---------------------------------------------------------------------------
# R6 / contexts
# ---------------------------------------------------------------------------


class TestR6:
    def test_R6_same_rules_in_every_context(self):
        # aggressive savings widens both edges by 2 F: the guard works on the WIDENED edges
        engine, clock = _scene(day="mild", mode="cool", indoor=71.0, ran="cool", aggressive_savings=True)
        assert engine._last_comfort_band.floor == _FLOOR - 2.0
        assert _mode(engine) == "cool"  # 71 is mid-gap of the widened 66-76 band: nothing to switch
        _reactive(engine, clock, 65.0)  # depth 1 under the widened floor 66: held
        _apply(engine, clock, "mild", 65.0)
        assert _mode(engine) == "cool"
        _apply(engine, clock, "mild", 63.0)  # depth 3 > 2
        assert _mode(engine) == "heat"

        # wake-up step: the floor rises (sleep_heat -> comfort_heat) right after an overnight cool run
        e2 = _make_engine(
            indoor=68.5,
            mode="cool",
            sleep_time="22:00",
            wake_time="06:30",
            sleep_heat=66.0,
            sleep_cool=76.0,
        )
        _stamp_ran(e2, "cool", 45.0, now=_T0)
        clk2 = _Clock()
        _apply(e2, clk2, "mild", 67.0)  # 1 F under the comfort floor right after the cool run
        assert _mode(e2) == "cool"

        # the pre-cool morning writer feeds the mode-change clock like any other writer
        e3 = _make_engine(indoor=70.0, mode="off")
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock()), e3._mode_change_context("tou"):
            _run(e3._set_hvac_mode("cool", reason="pre-cool"))
        e3._test_state.state = "cool"
        _apply(e3, _Clock(_T0 + timedelta(minutes=30)), "mild", 67.0)
        assert _mode(e3) == "cool"

    @pytest.mark.parametrize("supported", [["off", "heat"], ["off", "cool"]], ids=["heat-only", "cool-only"])
    def test_R6_single_mode_thermostat_unaffected(self, supported):
        mode = supported[1]
        engine = _make_engine(indoor=67.0, mode=mode, hvac_modes=supported)
        _stamp_ran(engine, "cool" if mode == "cool" else "heat", 10.0)
        _apply(engine, _Clock(), "mild", 67.0)
        assert engine._last_comfort_hold is None
        assert _mode(engine) == mode
        assert set(_mode_writes(engine)) <= {mode}

    def test_leaf_single_mode_ignores_the_guard(self):
        for kw, family in (({"supports_cool": False}, "heating"), ({"supports_heat": False}, "cooling")):
            d = decide_comfort_family(_leaf(indoor=67.0, current="cooling", ran=5.0, **kw))
            assert d.target_family == family
            assert d.held is False


# ---------------------------------------------------------------------------
# R7 -- the thermal model is never read
# ---------------------------------------------------------------------------


class TestR7:
    @pytest.mark.parametrize("confidence", ["none", "low", "high", "stale"])
    def test_R7_model_confidence_does_not_change_control(self, confidence):
        outcomes = []
        for model_conf in ("none", confidence):
            engine = _make_engine(indoor=67.0, mode="cool")
            engine._thermal_model = {
                "confidence": model_conf,
                "confidence_k_passive": model_conf,
                "confidence_k_hvac": model_conf,
                "k_passive": -0.05 if model_conf != "none" else 0.0,
                "k_active_heat": 5.0,
                "k_active_cool": -5.0,
            }
            _stamp_ran(engine, "cool", 30.0)
            _apply(engine, _Clock(), "hot", 67.0)
            outcomes.append((_mode(engine), engine._last_comfort_hold is not None))
        assert outcomes[0] == outcomes[1] == ("cool", True)

    def test_leaf_and_inputs_have_no_model_field(self):
        names = {f for f in ComfortFamilyInputs.__dataclass_fields__} | {
            f for f in ComfortFamilyFsmInputs.__dataclass_fields__
        }
        assert not [n for n in names if "model" in n or "k_passive" in n or "confidence" in n]


# ---------------------------------------------------------------------------
# R8 -- logged once per genuine hold; the hold does not move the dwell clock
# ---------------------------------------------------------------------------


class TestR8:
    def test_R8_every_decision_logged_and_event_once(self, caplog):
        engine = _make_engine(indoor=67.0, mode="cool")
        _stamp_ran(engine, "cool", 20.0)
        clock = _Clock()
        caplog.set_level(logging.DEBUG, logger="custom_components.climate_advisor.automation")
        for i in range(5):  # five consecutive readings of the same hold
            _apply(engine, clock, "hot", 67.0 - 0.1 * i)
            clock.advance(seconds=30)
        held_info = [r for r in caplog.records if r.levelno == logging.INFO and "Comfort edge switch held" in r.message]
        held_debug = [
            r for r in caplog.records if r.levelno == logging.DEBUG and "Comfort edge switch held" in r.message
        ]
        assert len(held_info) == 1
        assert len(held_debug) == 4
        msg = held_info[0].getMessage()
        for token in (
            "wants=heating",
            "current=cooling",
            "depth=",
            "tolerance=5.0",
            "opposite_ran_min_ago=",
            "native=cooling",
        ):
            assert token in msg, token

        # the hold ends (deeper breach) -> a later, fresh hold logs INFO again
        _apply(engine, clock, "hot", 60.0)
        assert _mode(engine) == "heat"
        assert engine._last_comfort_hold is None

    def test_a_genuine_switch_is_still_logged_and_emitted(self, caplog):
        engine = _make_engine(indoor=62.0, mode="cool")
        events: list[str] = []
        engine._emit_event_callback = lambda n, d: events.append(n)
        caplog.set_level(logging.INFO, logger="custom_components.climate_advisor.automation")
        _apply(engine, _Clock(), "hot", 62.0)
        assert "comfort_family_switch" in events
        assert any("Comfort edge switched" in r.message for r in caplog.records)

    def test_hold_data_is_exposed_for_the_activity_event(self):
        engine, _ = _scene(day="hot", mode="cool", indoor=66.0, ran="cool", ran_min=25.0)
        hold = engine._last_comfort_hold
        assert hold["candidate_family"] == "heating"
        assert hold["current_family"] == "cooling"
        assert hold["depth_f"] == pytest.approx(2.0)
        assert hold["tolerance_f"] == 5.0
        assert hold["opposite_ran_min_ago"] == pytest.approx(25.0)
        assert hold["window_min"] == 120.0
        assert hold["indoor_f"] == 66.0
        assert (hold["floor_f"], hold["ceiling_f"]) == (_FLOOR, _CEIL)

    def test_hold_does_not_arm_or_reset_the_dwell_clock(self):
        engine, clock = _scene(day="hot", mode="cool", indoor=67.0, ran="cool")
        assert engine._last_comfort_hold is not None
        dwell = engine._comfort_family_dwell_state
        assert dwell is None or dwell.dwell_since is None
        # ...so a later genuine deep switch is not locked
        _reactive(engine, clock, 62.0)
        assert _mode(engine) == "heat"

    def test_reactive_check_treats_a_hold_as_no_switch(self):
        engine, clock = _scene(day="hot", mode="cool", indoor=72.0, ran="cool")
        before = len(engine.hass.services.async_call.call_args_list)
        for reading in (67.0, 66.5, 66.0):
            _reactive(engine, clock, reading)
        assert len(engine.hass.services.async_call.call_args_list) == before, "no service call on a held reading"
        would, result = engine._comfort_family_would_switch(
            "cool", floor=_FLOOR, ceiling=_CEIL, now=clock.now, day_type="hot", indoor=67.0
        )
        assert would is False
        assert result.decision.held is True
        assert result.decision.outcome is ComfortFamilyOutcome.HOLD
        assert result.locked_out is False
        assert result.dwell_state.dwell_since is None


# ---------------------------------------------------------------------------
# R9 -- the spec's variable table matches const.py
# ---------------------------------------------------------------------------


class TestR9:
    def test_R9_variable_table_matches_const(self):
        const = importlib.import_module("custom_components.climate_advisor.const")
        spec = (Path(__file__).resolve().parent.parent / "docs" / "comfort-edge-selection-spec.md").read_text(
            encoding="utf-8"
        )
        rows = {}
        for line in spec.splitlines():
            m = re.match(
                r"\|\s*`(comfort_[a-z_]+)`\s*\|\s*([0-9.]+)\s*\|\s*[A-Za-z]+\s*\|\s*([0-9.]+) - ([0-9.]+)\s*\|", line
            )
            if m:
                rows[m.group(1)] = (float(m.group(2)), float(m.group(3)), float(m.group(4)))
        for day in ("hot", "warm", "mild", "cool", "cold"):
            key = f"comfort_deadband_{day}_f"
            assert key in rows, f"{key} missing from the spec variable table"
            default = getattr(const, f"DEFAULT_COMFORT_DEADBAND_{day.upper()}_F")
            lo = getattr(const, f"COMFORT_DEADBAND_{day.upper()}_MIN_F")
            hi = getattr(const, f"COMFORT_DEADBAND_{day.upper()}_MAX_F")
            assert rows[key] == (default, lo, hi)
            assert key in const.CONFIG_METADATA
        assert rows["comfort_family_recency_window_min"][0] == const.DEFAULT_COMFORT_FAMILY_RECENCY_WINDOW_MIN
        assert rows["comfort_family_recency_window_min"][1:] == (
            const.COMFORT_FAMILY_RECENCY_WINDOW_MIN_MIN,
            const.COMFORT_FAMILY_RECENCY_WINDOW_MIN_MAX,
        )
        assert COMFORT_MIN_ARMING_MARGIN_F == 2.0
        assert "COMFORT_MIN_ARMING_MARGIN_F" in spec
        assert const.COMFORT_MODE_SWITCH_MIN_INTERVAL_S == 600.0

    def test_arming_margin_is_fixed_not_the_deadband(self):
        for day, expected_tol in (("hot", 5.0), ("mild", 2.0)):
            engine = _make_engine(indoor=70.0, mode="cool")
            inputs, _ = engine._comfort_family_inputs(
                "cool", floor=_FLOOR, ceiling=_CEIL, now=_T0, day_type=day, indoor=70.0
            )
            assert inputs.margin_f == COMFORT_MIN_ARMING_MARGIN_F
            assert inputs.tolerance_f == expected_tol

    def test_setting_descriptions_use_the_new_meaning(self):
        const = importlib.import_module("custom_components.climate_advisor.const")
        base = Path(const.__file__).parent
        for day in ("hot", "warm", "mild", "cool", "cold"):
            meta = const.CONFIG_METADATA[f"comfort_deadband_{day}_f"]
            assert "Overshoot Tolerance" in meta["label"]
            assert "arm" in meta["description"] and "pre-armed" in meta["description"]
            for fn in ("strings.json", "translations/en.json"):
                data = json.loads((base / fn).read_text(encoding="utf-8"))
                opts = data["options"]["step"]
                found = [
                    step["data"][f"comfort_deadband_{day}_f"]
                    for step in opts.values()
                    if f"comfort_deadband_{day}_f" in step.get("data", {})
                ]
                assert found and all("Overshoot Tolerance" in f for f in found)

    def test_stall_detector_margins_are_unchanged_by_the_meaning_change(self):
        from custom_components.climate_advisor.comfort_stall_decision import stall_margins

        engine = _make_engine(indoor=70.0, mode="cool")
        assert engine._comfort_deadband_for_day_type("hot") == 5.0
        assert engine._comfort_deadband_for_day_type("mild") == 2.0
        assert engine._comfort_deadband_for_day_type(None) == 2.0
        # against-grain direction waits deadband + 1 F; the native direction keeps the flat 1 F
        assert stall_margins("cooling", 5.0, 1.0) == (6.0, 1.0)
        assert stall_margins("heating", 5.0, 1.0) == (1.0, 6.0)


# ---------------------------------------------------------------------------
# the pure leaf / FSM
# ---------------------------------------------------------------------------


def _leaf(
    *,
    indoor,
    current,
    ran,
    tolerance=2.0,
    native=None,
    window=120.0,
    floor=_FLOOR,
    ceiling=_CEIL,
    **kw,
) -> ComfortFamilyInputs:
    return ComfortFamilyInputs(
        current_family=current,
        default_family="cooling",
        indoor=indoor,
        floor=floor,
        ceiling=ceiling,
        margin_f=COMFORT_MIN_ARMING_MARGIN_F,
        natural_vent_active=False,
        whf_owns_hvac=False,
        now=_T0,
        tolerance_f=tolerance,
        native_family=native,
        opposite_ran_min_ago=ran,
        recency_window_min=window,
        **kw,
    )


class TestLeaf:
    def test_hold_carries_its_explanation(self):
        d = decide_comfort_family(_leaf(indoor=67.0, current="cooling", ran=30.0))
        assert d.outcome is ComfortFamilyOutcome.HOLD
        assert d.held and d.target_family == "cooling" and d.hold_candidate_family == "heating"
        assert (d.hold_depth_f, d.hold_tolerance_f, d.opposite_ran_min_ago, d.hold_window_min) == (
            1.0,
            2.0,
            30.0,
            120.0,
        )
        assert d.reason.startswith("settling guard")

    @pytest.mark.parametrize(
        ("indoor", "current", "native", "ran", "tol", "held"),
        [
            (67.0, "cooling", None, 30.0, 2.0, True),
            (66.0, "cooling", None, 30.0, 2.0, True),  # depth == tolerance
            (65.99, "cooling", None, 30.0, 2.0, False),  # deeper
            (67.0, "cooling", "heating", 30.0, 2.0, False),  # native target
            (67.0, "cooling", "cooling", 30.0, 2.0, True),  # against the grain
            (67.0, "cooling", None, None, 2.0, False),  # unknown run
            (67.0, "cooling", None, 120.0, 2.0, False),  # window edge is strict
            (67.0, "cooling", None, 0.0, 2.0, True),
            (67.0, None, None, 30.0, 2.0, False),  # unknown current family: adoption
            (67.0, "heating", None, 30.0, 2.0, False),  # already heating: nothing to hold
            (74.5, "heating", None, 30.0, 2.0, True),  # cool mirror
            (76.0, "heating", None, 30.0, 2.0, True),
            (76.01, "heating", None, 30.0, 2.0, False),
            (74.5, "heating", "cooling", 30.0, 2.0, False),
        ],
    )
    def test_matrix(self, indoor, current, native, ran, tol, held):
        d = decide_comfort_family(_leaf(indoor=indoor, current=current, native=native, ran=ran, tolerance=tol))
        assert d.held is held

    def test_default_inputs_disable_the_guard(self):
        d = decide_comfort_family(
            ComfortFamilyInputs(
                current_family="cooling",
                default_family="cooling",
                indoor=67.0,
                floor=_FLOOR,
                ceiling=_CEIL,
                margin_f=2.0,
                natural_vent_active=False,
                whf_owns_hvac=False,
                now=_T0,
            )
        )
        assert d.outcome is ComfortFamilyOutcome.SWITCH and not d.held

    def test_fsm_passes_the_guard_through_and_hold_never_moves_the_clock(self):
        inputs = ComfortFamilyFsmInputs(
            default_family="cooling",
            indoor=67.0,
            floor=_FLOOR,
            ceiling=_CEIL,
            margin_f=2.0,
            natural_vent_active=False,
            whf_owns_hvac=False,
            now=_T0,
            min_dwell_seconds=600.0,
            tolerance_f=2.0,
            opposite_ran_min_ago=10.0,
            recency_window_min=120.0,
        )
        t = fsm_transition(ComfortFamilyState.COOLING, ComfortFamilyEvent(ComfortFamilyEventKind.TICK, inputs))
        assert t.decision.held and not t.changed and not t.locked_out
        assert t.dwell_state.dwell_since is None


# ---------------------------------------------------------------------------
# Which things count as "the opposite side ran": deliberate commands and real equipment only
# ---------------------------------------------------------------------------


class TestWhatCountsAsRan:
    @staticmethod
    def _after_command(reason: str, mode: str, indoor: float, day: str = "mild"):
        """A ``reason`` write of ``mode`` 15 min ago (no equipment event), then a 2 F overshoot."""
        other = "heat" if mode == "cool" else "cool"
        engine = _make_engine(indoor=71.0, mode=other)
        clock = _Clock()
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), engine._mode_change_context(reason):
            _run(engine._set_hvac_mode(mode, reason=reason))
        clock.advance(minutes=15)
        _apply(engine, clock, day, indoor)
        return engine

    @pytest.mark.parametrize("reason", ["selector", "restore", "startup", "pause", "nat_vent", "other"])
    @pytest.mark.parametrize(("mode", "indoor", "expected"), [("cool", 66.0, "heat"), ("heat", 76.0, "cool")])
    def test_default_or_restore_command_does_not_count_as_ran(self, reason, mode, indoor, expected):
        engine = self._after_command(reason, mode, indoor)
        assert _mode(engine) == expected
        assert engine._last_comfort_hold is None

    @pytest.mark.parametrize("reason", ["tou", "ode_guard", "user"])
    @pytest.mark.parametrize(("mode", "indoor"), [("cool", 66.0), ("heat", 76.0)])
    def test_deliberate_command_counts_as_ran(self, reason, mode, indoor):
        engine = self._after_command(reason, mode, indoor)
        assert _mode(engine) == mode
        assert engine._last_comfort_hold is not None

    @pytest.mark.parametrize(("mode", "indoor"), [("cool", 66.0), ("heat", 76.0)])
    def test_equipment_that_ran_counts_even_after_a_restore_command(self, mode, indoor):
        other = "heat" if mode == "cool" else "cool"
        engine = _make_engine(indoor=71.0, mode=other)
        clock = _Clock()
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), engine._mode_change_context("restore"):
            _run(engine._set_hvac_mode(mode, reason="restore"))
        _stamp_ran(engine, mode, 10.0, now=clock.now + timedelta(minutes=15))
        clock.advance(minutes=15)
        _apply(engine, clock, "mild", indoor)
        assert _mode(engine) == mode
        assert engine._last_comfort_hold is not None

    def test_mode_set_reason_filter_is_generic(self):
        engine = _make_engine(indoor=70.0, mode="off")
        clock = _Clock()
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), engine._mode_change_context("restore"):
            _run(engine._set_hvac_mode("cool", reason="restore"))
        later = _T0 + timedelta(minutes=5)
        assert engine.minutes_since_mode_set("cool", later) == 5.0
        assert engine.minutes_since_mode_set("cool", later, frozenset({"user", "tou"})) is None
