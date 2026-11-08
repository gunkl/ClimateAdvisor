"""Issue #1410 (decision C23): the Away/Vacation time-of-use split is on the thermostat's step and frozen per window.

Occupant view: an empty house pre-cooling for a high-cost period was told cool 77.3875 F, then 82.6125 F at the
period start, and the value crept every cycle as the indoor reading changed. Now the bank target and the widened edge
are values a person could set (the thermostat's own step, in its unit; a whole degree / 0.5 C when it advertises no
step), and they are fixed when the window opens until it ends or the occupancy/schedule changes. Home/Guest banks are
unchanged.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.const import OCCUPANCY_AWAY, OCCUPANCY_VACATION  # noqa: E402
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402
from custom_components.climate_advisor.scheduler import (  # noqa: E402
    COST_TAG_HIGH,
    Schedule,
    TOUPhase,
    quantize_split_delta,
    resolve_tou_away_vacation_phase,
    resolve_tou_phase,
)
from tests.test_tou_away_vacation_coordinator import _build_coordinator  # noqa: E402

# k_passive -0.1, outdoor 90 F, 16:00-20:00 (4 h): indoor 79 -> drift 4.4 F, half 2.2 (raw target 76.8, widened 81.2).
_MODEL = {"k_passive": -0.1, "confidence_k_passive": "high", "k_active_cool": -1.0, "k_active_heat": 1.0}
_SCHED = Schedule(id="s1", name="s", days=("all",), start="16:00", end="20:00", cost_tag=COST_TAG_HIGH)
_CFG = {"setback_heat": 63.0, "setback_cool": 79.0}


def _dt(h: int, m: int = 0, day: int = 5) -> datetime:
    return datetime(2026, 1, day, h, m, tzinfo=UTC)


def _resolve(indoor: float, now: datetime, **kw):
    cfg = kw.pop("config", _CFG)
    occ = kw.pop("occupancy", OCCUPANCY_AWAY)
    mode = kw.pop("mode", "cool")
    return resolve_tou_away_vacation_phase([_SCHED], now, indoor, 90.0, mode, _MODEL, cfg, occ, **kw)


class TestQuantize:
    @pytest.mark.parametrize(
        ("step", "unit", "target", "expansion"),
        [
            (1.0, "fahrenheit", 77.0, 3.0),  # 77.3875 -> 77, 82.6125 -> 83 (the issue's numbers)
            (0.5, "fahrenheit", 77.5, 2.5),  # 77.3875 -> 77.5, 82.6125 -> 82.5
            (0.5, "celsius", 77.0, 2.4),  # 25.22 C -> 25.0 C (77 F), 28.12 C -> 28.0 C (82.4 F)
            (1.0, "celsius", 77.0, 2.4),  # 25 C and 28 C
        ],
    )
    def test_cool_split_on_the_step(self, step, unit, target, expansion):
        t, e = quantize_split_delta(80.0, 2.6125, "cool", step, unit)
        assert t == pytest.approx(target)
        assert e == pytest.approx(expansion)

    def test_heat_mirror(self):
        t, e = quantize_split_delta(60.0, 2.6125, "heat", 1.0, "fahrenheit")
        assert (t, e) == (63.0, 3.0)  # preheat 62.6125 -> 63, floor 57.3875 -> 57

    def test_target_and_widened_edge_are_on_the_step_in_celsius(self):
        t, e = quantize_split_delta(80.0, 2.6125, "cool", 0.5, "celsius")
        for value_f in (t, 80.0 + e):
            c = (value_f - 32.0) * 5.0 / 9.0
            assert c / 0.5 == pytest.approx(round(c / 0.5), abs=1e-6)

    def test_ties_round_half_up(self):
        assert quantize_split_delta(80.0, 2.5, "cool", 1.0, "fahrenheit") == (78.0, 3.0)  # 77.5 -> 78, 82.5 -> 83

    def test_widening_never_negative(self):
        # An off-grid edge with a tiny half: the widened edge would snap inside the edge -> no widening.
        assert quantize_split_delta(79.4, 0.05, "cool", 1.0, "fahrenheit")[1] == 0.0


class TestResolver:
    def test_unknown_step_is_a_whole_degree_fahrenheit(self):
        res = _resolve(79.0, _dt(14))
        assert (res.target, res.expansion_f) == (77.0, 2.0)
        assert res.phase == TOUPhase.PRECONDITIONING  # lead from the snapped depth 2 F: 156 min -> 13:24

    def test_advertised_half_step(self):
        res = _resolve(78.0, _dt(14), target_temp_step=0.5)  # half 2.4: 76.6 -> 76.5, 81.4 -> 81.5
        assert (res.target, res.expansion_f) == (76.5, 2.5)

    def test_unknown_step_on_a_celsius_install_is_half_a_degree(self):
        res = _resolve(79.0, _dt(14), config={**_CFG, "temp_unit": "celsius"})
        for value_f in (res.target, 79.0 + res.expansion_f):
            c = (value_f - 32.0) * 5.0 / 9.0
            assert c * 2 == pytest.approx(round(c * 2), abs=1e-6)

    def test_bad_step_falls_back_to_default(self):
        assert _resolve(78.0, _dt(14), target_temp_step="abc").target == 77.0
        assert _resolve(78.0, _dt(14), target_temp_step=0).target == 77.0

    def test_frozen_resolution_is_reused_while_its_key_matches(self):
        first = _resolve(79.0, _dt(14))
        later = _resolve(77.0, _dt(15), frozen=first)  # unfrozen: half 2.6 -> 76 / 3
        assert later.frozen and (later.target, later.expansion_f) == (77.0, 2.0)
        assert later.precondition_start == first.precondition_start
        assert later.phase == TOUPhase.PRECONDITIONING
        inside = _resolve(77.0, _dt(17), frozen=first)  # the period itself: same widening
        assert inside.frozen and inside.expansion_f == 2.0 and inside.phase == TOUPhase.NONE

    def test_frozen_is_ignored_when_the_key_changes(self):
        first = _resolve(79.0, _dt(14))
        assert not _resolve(77.0, _dt(15), frozen=first, occupancy=OCCUPANCY_VACATION).frozen
        assert not _resolve(77.0, _dt(15), frozen=first, target_temp_step=0.5).frozen
        assert not _resolve(77.0, _dt(15), frozen=first, config={**_CFG, "setback_cool": 80.0}).frozen
        assert _resolve(77.0, _dt(15), frozen=first).frozen

    def test_frozen_is_ignored_for_the_next_days_window(self):
        first = _resolve(79.0, _dt(14))
        nxt = _resolve(77.0, _dt(14, day=6), frozen=first)
        assert not nxt.frozen and nxt.target == 76.0


def _coord(occupancy: str = OCCUPANCY_AWAY, step: object = None) -> ClimateAdvisorCoordinator:
    coord = _build_coordinator(occupancy)
    coord._tou_av_frozen = None
    coord._tou_precondition_sticky = None
    coord._last_outdoor_temp = 90.0
    coord.control_thermal_model = lambda _m: dict(_MODEL)
    attrs = {} if step is None else {"target_temp_step": step}
    coord.hass.states.get = MagicMock(return_value=SimpleNamespace(state="cool", attributes=attrs))
    return coord


def _cycle(coord, now: datetime, indoor: float):
    coord._get_indoor_temp = MagicMock(return_value=indoor)
    with (
        patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=now),
        patch("custom_components.climate_advisor.coordinator.async_call_later", return_value=MagicMock()),
    ):
        coord._resolve_tou_schedule_state()
    return coord._tou_phase_resolution


class TestCoordinatorFreeze:
    def test_indoor_change_does_not_move_the_bank_or_the_widening(self):
        coord = _coord()
        assert (_cycle(coord, _dt(14), 79.0).target, coord._tou_av_frozen.expansion_f) == (77.0, 2.0)
        assert _cycle(coord, _dt(15), 77.0).target == 77.0  # unfrozen: 76
        res = _cycle(coord, _dt(17), 77.0)  # inside the period
        assert res.expansion_f == 2.0  # unfrozen: 3
        assert coord._tou_av_window.expansion_f == 2.0  # what the band write widens by

    def test_not_frozen_before_the_window_opens(self):
        coord = _coord()
        assert _cycle(coord, _dt(12), 79.0).phase == TOUPhase.NONE
        assert coord._tou_av_frozen is None

    def test_window_end_drops_the_freeze(self):
        coord = _coord()
        _cycle(coord, _dt(14), 79.0)
        _cycle(coord, _dt(20, 30), 77.0)  # period over, the next one is outside the lookahead
        assert coord._tou_av_frozen is None
        assert _cycle(coord, _dt(14, day=6), 77.0).target == 76.0  # the next window re-derives

    def test_occupancy_change_drops_the_freeze(self):
        coord = _coord()
        _cycle(coord, _dt(14), 79.0)
        coord._occupancy_mode = "home"
        _cycle(coord, _dt(14, 30), 77.0)
        assert coord._tou_av_frozen is None
        coord._occupancy_mode = OCCUPANCY_AWAY
        assert _cycle(coord, _dt(15), 77.0).target == 76.0  # re-derived for the new away period

    def test_away_to_vacation_re_derives(self):
        coord = _coord()
        _cycle(coord, _dt(14), 79.0)
        coord._occupancy_mode = OCCUPANCY_VACATION
        res = _cycle(coord, _dt(14, 30), 79.0)
        assert not res.frozen and res.split_key[4] == OCCUPANCY_VACATION

    def test_restart_re_derives_once_then_freezes(self):
        _cycle(_coord(), _dt(14), 79.0)
        fresh = _coord()  # a restart: no TOU state is persisted
        assert _cycle(fresh, _dt(15), 77.0).target == 76.0  # re-derived once from the reading after the restart
        assert _cycle(fresh, _dt(15, 30), 79.0).target == 76.0  # then frozen

    def test_thermostat_step_is_read_from_its_state(self):
        coord = _coord(step=0.5)
        assert _cycle(coord, _dt(14), 78.0).target == 76.5


class TestHomeGuestUnchanged:
    def test_home_bank_is_not_quantized_or_frozen(self):
        cfg = {**_CFG, "comfort_heat": 68.4, "comfort_cool": 76.0}
        res = resolve_tou_phase([_SCHED], _dt(15, 30), 76.0, "cool", _MODEL, cfg)
        assert res.target == 68.4  # the comfort floor as configured, no step applied
        assert res.split_key is None and not res.frozen and res.expansion_f is None

    def test_home_coordinator_cycle_keeps_no_freeze(self):
        coord = _coord(occupancy="home")
        coord.config["comfort_heat"] = 68.4
        res = _cycle(coord, _dt(15, 30), 76.0)
        assert coord._tou_av_frozen is None and res.split_key is None
        assert res.target == 68.4
