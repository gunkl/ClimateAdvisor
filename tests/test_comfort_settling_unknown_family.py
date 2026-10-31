"""Issue #1149: the settling guard also applies when the thermostat's live family is unknown.

Occupant framing: the AC has just cooled the house and the occupant switches the thermostat Off (or it is
in heat_cool / auto, or it just came back online). The house settles a degree or two under the comfort
minimum. Climate Advisor used to treat "no live heat/cool mode" as "nothing to hold" and started the
furnace at once. The guard now treats the family that ran (or was deliberately commanded) most recently
inside ``comfort_family_recency_window_min`` as the family being left, so a shallow dip is held exactly as
when the thermostat is still in that mode. See ``docs/comfort-edge-selection-spec.md`` (R2, section 5).

Real ``AutomationEngine`` + the fake thermostat of ``test_comfort_settling_guard``; assertions are on the
hvac mode actually written to the thermostat. Nothing here re-implements production logic.
"""

from __future__ import annotations

import pytest

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
from tests.test_comfort_settling_guard import (
    _CEIL,
    _FLOOR,
    _T0,
    _apply,
    _Clock,
    _make_engine,
    _mode,
    _mode_writes,
    _reactive,
    _scene,
    _stamp_ran,
)

_UNKNOWN_MODES = ["off", "heat_cool", "auto", "unavailable"]


# ---------------------------------------------------------------------------
# Real engine: thermostat family unknown
# ---------------------------------------------------------------------------


class TestUnknownFamilyHoldsAfterTheOppositeRan:
    @pytest.mark.parametrize("mode", _UNKNOWN_MODES)
    @pytest.mark.parametrize(("day", "indoor"), [("mild", 67.0), ("hot", 64.0), ("warm", 67.0)])
    def test_shallow_dip_after_cool_ran_does_not_heat(self, mode, day, indoor):
        engine, _ = _scene(day=day, mode=mode, indoor=indoor, ran="cool", ran_min=30.0)
        assert "heat" not in _mode_writes(engine), "a shallow dip right after the AC ran must not start the furnace"
        assert _mode(engine) == "cool", "CA arms the side that ran (cool at the ceiling); the AC idles below it"
        hold = engine._last_comfort_hold
        assert hold is not None
        assert hold["candidate_family"] == "heating"
        assert hold["current_family"] == "cooling"
        assert hold["zone"] == "breach"

    @pytest.mark.parametrize("mode", _UNKNOWN_MODES)
    def test_margin_zone_after_cool_ran_does_not_heat(self, mode):
        engine, _ = _scene(day="mild", mode=mode, indoor=69.0, ran="cool", ran_min=30.0)
        assert "heat" not in _mode_writes(engine)
        assert _mode(engine) == "cool"
        assert engine._last_comfort_hold is not None and engine._last_comfort_hold["zone"] == "margin"

    @pytest.mark.parametrize("mode", _UNKNOWN_MODES)
    @pytest.mark.parametrize(("day", "indoor"), [("mild", 75.0), ("cold", 78.0), ("cool", 75.0)])
    def test_mirror_shallow_overshoot_after_heat_ran_does_not_cool(self, mode, day, indoor):
        engine, _ = _scene(day=day, mode=mode, indoor=indoor, ran="heat", ran_min=30.0)
        assert "cool" not in _mode_writes(engine), "a shallow overshoot right after the furnace ran must not run the AC"
        assert _mode(engine) == "heat"
        hold = engine._last_comfort_hold
        assert hold is not None and hold["candidate_family"] == "cooling" and hold["current_family"] == "heating"

    def test_the_reproduction_from_issue_1149(self):
        """Mild day, AC ran, occupant switched Off, 06:30 refresh at 68.0 -> 67.0 reading: no furnace."""
        engine, _ = _scene(day="mild", mode="off", indoor=68.0, ran="cool", ran_min=70.0)
        assert "heat" not in _mode_writes(engine)
        assert _mode(engine) == "cool"

    @pytest.mark.parametrize("mode", _UNKNOWN_MODES)
    def test_reactive_reading_is_held_too(self, mode):
        engine, clock = _scene(day="mild", mode="cool", indoor=71.0, ran="cool", ran_min=30.0)
        engine._test_state.state = mode  # the occupant switched it Off / it went heat_cool / unavailable
        _reactive(engine, clock, 67.0)
        assert "heat" not in _mode_writes(engine)

    @pytest.mark.parametrize("mode", _UNKNOWN_MODES)
    def test_reactive_mirror_is_held_too(self, mode):
        engine, clock = _scene(day="mild", mode="heat", indoor=71.0, ran="heat", ran_min=30.0)
        engine._test_state.state = mode
        _reactive(engine, clock, 75.0)
        assert "cool" not in _mode_writes(engine)


class TestUnknownFamilyReleases:
    @pytest.mark.parametrize("mode", _UNKNOWN_MODES)
    def test_r1_deeper_than_the_tolerance_heats_at_once(self, mode):
        engine, _ = _scene(day="mild", mode=mode, indoor=65.5, ran="cool", ran_min=30.0)  # depth 2.5 > 2
        assert _mode(engine) == "heat"
        assert engine._last_comfort_hold is None

    @pytest.mark.parametrize("mode", _UNKNOWN_MODES)
    def test_r1_mirror_cools_at_once(self, mode):
        engine, _ = _scene(day="mild", mode=mode, indoor=76.5, ran="heat", ran_min=30.0)
        assert _mode(engine) == "cool"
        assert engine._last_comfort_hold is None

    @pytest.mark.parametrize("mode", _UNKNOWN_MODES)
    def test_window_lapsed_heats(self, mode):
        engine, _ = _scene(day="mild", mode=mode, indoor=67.0, ran="cool", ran_min=130.0)
        assert _mode(engine) == "heat"
        assert engine._last_comfort_hold is None

    def test_hold_ends_when_the_window_lapses(self):
        engine, clock = _scene(day="mild", mode="off", indoor=67.0, ran="cool", ran_min=100.0)
        assert _mode(engine) == "cool"
        clock.advance(minutes=30)  # 130 min since the AC ran
        engine._test_state.state = "off"
        _reactive(engine, clock, 67.0)
        assert _mode(engine) == "heat"

    @pytest.mark.parametrize("mode", _UNKNOWN_MODES)
    def test_native_side_is_never_held(self, mode):
        # cold day: heat is native (tolerance 0) even though the AC ran 30 min ago
        engine, _ = _scene(day="cold", mode=mode, indoor=67.0, ran="cool", ran_min=30.0)
        assert _mode(engine) == "heat"
        assert engine._last_comfort_hold is None
        # hot day mirror: cool is native even though the furnace ran 30 min ago
        engine2, _ = _scene(day="hot", mode=mode, indoor=75.0, ran="heat", ran_min=30.0)
        assert _mode(engine2) == "cool"
        assert engine2._last_comfort_hold is None

    def test_nothing_ran_is_todays_behaviour(self):
        engine, _ = _scene(day="mild", mode="off", indoor=67.0, ran=None)
        assert _mode(engine) == "heat"
        assert engine._last_comfort_hold is None
        # mid-band, nothing ran: the day's default family is adopted (unchanged)
        engine2, _ = _scene(day="hot", mode="off", indoor=71.0, ran=None)
        assert _mode(engine2) == "cool"
        assert engine2._last_comfort_hold is None


class TestBothFamiliesRan:
    def test_newest_family_wins_and_holds(self):
        engine = _make_engine(indoor=67.0, mode="off")
        clock = _Clock()
        _stamp_ran(engine, "heat", 90.0)
        _stamp_ran(engine, "cool", 20.0)  # the AC ran last -> it is the side being left
        _apply(engine, clock, "mild", 67.0)
        assert "heat" not in _mode_writes(engine)
        assert _mode(engine) == "cool"

    def test_newest_family_equal_to_the_target_means_no_hold(self):
        engine = _make_engine(indoor=67.0, mode="off")
        clock = _Clock()
        _stamp_ran(engine, "cool", 90.0)
        _stamp_ran(engine, "heat", 20.0)  # the furnace ran last: heating is not "against" anything
        _apply(engine, clock, "mild", 67.0)
        assert _mode(engine) == "heat"
        assert engine._last_comfort_hold is None


class TestKnownFamilyUnchanged:
    def test_known_cool_still_holds_and_known_heat_still_adopts_normally(self):
        engine, _ = _scene(day="mild", mode="cool", indoor=67.0, ran="cool", ran_min=30.0)
        assert "heat" not in _mode_writes(engine) and _mode(engine) == "cool"
        assert engine._last_comfort_hold["current_family"] == "cooling"
        # live heat with only the AC having run: the family being left (heat) did not run -> switch is nothing
        engine2, _ = _scene(day="mild", mode="heat", indoor=67.0, ran="cool", ran_min=30.0)
        assert _mode(engine2) == "heat"
        assert engine2._last_comfort_hold is None


# ---------------------------------------------------------------------------
# Leaf and FSM
# ---------------------------------------------------------------------------


def _leaf(**kw):
    base = {
        "current_family": None,
        "default_family": "cooling",
        "indoor": 67.0,
        "floor": _FLOOR,
        "ceiling": _CEIL,
        "margin_f": COMFORT_MIN_ARMING_MARGIN_F,
        "natural_vent_active": False,
        "whf_owns_hvac": False,
        "now": _T0,
        "tolerance_f": 2.0,
        "native_family": None,
        "opposite_ran_min_ago": 30.0,
        "recency_window_min": 120.0,
        "settling_from_family": "cooling",
    }
    base.update(kw)
    return ComfortFamilyInputs(**base)


class TestLeafSettlingFromFamily:
    def test_unknown_current_with_a_ran_family_holds_it(self):
        d = decide_comfort_family(_leaf())
        assert d.outcome is ComfortFamilyOutcome.HOLD and d.held
        assert d.target_family == "cooling" and d.hold_candidate_family == "heating"
        assert d.hold_zone == "breach" and d.hold_depth_f == pytest.approx(1.0)
        assert d.opposite_ran_min_ago == 30.0

    def test_margin_zone_and_cool_mirror(self):
        d = decide_comfort_family(_leaf(indoor=69.0))
        assert d.held and d.hold_zone == "margin" and d.target_family == "cooling"
        d2 = decide_comfort_family(_leaf(indoor=75.0, settling_from_family="heating"))
        assert d2.held and d2.target_family == "heating" and d2.hold_candidate_family == "cooling"

    def test_no_settling_family_is_plain_adoption(self):
        d = decide_comfort_family(_leaf(settling_from_family=None))
        assert not d.held and d.target_family == "heating"

    def test_settling_family_equal_to_the_target_is_not_held(self):
        d = decide_comfort_family(_leaf(settling_from_family="heating"))
        assert not d.held and d.target_family == "heating"

    @pytest.mark.parametrize(
        ("kw", "held"),
        [
            ({"indoor": 66.0}, True),  # depth == tolerance
            ({"indoor": 65.9}, False),  # R1
            ({"native_family": "heating"}, False),  # native side never held
            ({"opposite_ran_min_ago": 120.0}, False),  # window edge strict
            ({"opposite_ran_min_ago": None}, False),
            ({"recency_window_min": 0.0}, False),  # guard disabled (coordinator forecast call)
        ],
    )
    def test_same_release_rules_as_a_known_family(self, kw, held):
        assert decide_comfort_family(_leaf(**kw)).held is held

    def test_live_family_wins_over_the_stand_in(self):
        # current heating (already armed): nothing to switch; the stand-in must not matter
        d = decide_comfort_family(_leaf(current_family="heating", settling_from_family="cooling"))
        assert not d.held and d.target_family == "heating"
        # current cooling, stand-in says heating: the live family is the one being left
        d2 = decide_comfort_family(_leaf(current_family="cooling", settling_from_family="heating"))
        assert d2.held and d2.target_family == "cooling"

    def test_forecast_style_call_keeps_the_guard_disabled(self):
        """coordinator._build_predicted_indoor_future passes current_family=None and no recency window."""
        d = decide_comfort_family(
            ComfortFamilyInputs(
                current_family=None,
                default_family="cooling",
                indoor=67.0,
                floor=_FLOOR,
                ceiling=_CEIL,
                margin_f=COMFORT_MIN_ARMING_MARGIN_F,
                natural_vent_active=False,
                whf_owns_hvac=False,
                now=_T0,
            )
        )
        assert not d.held and d.target_family == "heating"


class TestFsmAdoptsTheHold:
    def test_unknown_state_adopts_the_held_family(self):
        inputs = ComfortFamilyFsmInputs(
            default_family="cooling",
            indoor=67.0,
            floor=_FLOOR,
            ceiling=_CEIL,
            margin_f=COMFORT_MIN_ARMING_MARGIN_F,
            natural_vent_active=False,
            whf_owns_hvac=False,
            now=_T0,
            min_dwell_seconds=600.0,
            tolerance_f=2.0,
            opposite_ran_min_ago=30.0,
            recency_window_min=120.0,
            settling_from_family="cooling",
        )
        result = fsm_transition(None, ComfortFamilyEvent(kind=ComfortFamilyEventKind.TICK, inputs=inputs))
        assert result.decision.held
        assert result.to_state is ComfortFamilyState.COOLING
        assert not result.locked_out
