"""Issue #1517 (owner decision 2026-11-11): on a zone whose primary prediction engine has its own house model, the
pre-heat / pre-cool / TOU-bank lead comes from stepping THAT model, not from the first-order leak-aware formula.

Occupant view: a heavy, sunny house takes much longer to reach the target than a single-rate formula says (the walls
keep pulling the heat or cool back out), so the bank must start earlier or the house is not ready when the high-price
period begins. A zone without such a model keeps the leak-aware formula; with no k or no outdoor, the plain one.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.nat_vent_plan import compute_nat_vent_plan
from custom_components.climate_advisor.ode_ceiling_guard import OdeCeilingGuardInputs, decide_ode_ceiling_guard
from custom_components.climate_advisor.prediction_engines import HouseStepper
from custom_components.climate_advisor.scheduler import (
    COST_TAG_HIGH,
    Schedule,
    resolve_tou_away_vacation_phase,
    resolve_tou_phase,
)
from custom_components.climate_advisor.thermal_lead_time import (
    compute_lead_minutes_from_rate,
    leak_aware_minutes,
    stepper_lead_minutes,
    stepper_lead_minutes_before,
)

_KW = {"min_minutes": 30.0, "max_minutes": 240.0, "safety_multiplier": 1.3, "fallback_minutes": 45.0}
_CFG = {"comfort_heat": 68.0, "comfort_cool": 76.0, "sleep_heat": 64.0, "sleep_cool": 72.0, "setback_cool": 80.0}
_START = datetime(2026, 1, 5, 16, 0, tzinfo=UTC)
_NOW = datetime(2026, 1, 5, 13, 0, tzinfo=UTC)
_SCHED = Schedule(id="s1", name="s", days=("all",), start="16:00", end="20:00", cost_tag=COST_TAG_HIGH)
_AFTERNOON = datetime(2026, 7, 15, 13, 0, tzinfo=UTC)


def make_stepper(*, k=-0.06, coupling=0.0, mass_rate=0.1, sun=0.0, heat=4.0, cool=4.0) -> HouseStepper:
    """A two-node house: the room leaks to outdoor at ``k``/h, is pulled toward a hidden mass (``coupling``/h), and
    gains ``sun`` degF/h at the solar peak (a daytime bump). Equipment off, as the real stepper's ``step``."""

    def step(t, mass, out, win, fan, hour):
        solar = sun * max(0.0, math.sin(math.pi * (hour - 6.0) / 12.0)) if 6.0 < hour < 18.0 else 0.0
        t_next = t + 0.25 * (k * (t - out) + coupling * (mass - t) + solar)
        mass_next = mass + 0.25 * mass_rate * (t - mass)
        return t_next, mass_next

    return HouseStepper(step=step, heat_gain=heat, cool_gain=cool)


def _lead(stepper, start, target, *, cooling, out=50.0, at=_AFTERNOON, **kw):
    return stepper_lead_minutes(
        stepper,
        start_temp=start,
        target_temp=target,
        cooling=cooling,
        outdoor_at=lambda _w: out,
        start_ts=at,
        **kw,
    )


class TestStepperLeadMinutes:
    def test_last_step_is_interpolated(self):
        flat = make_stepper(k=0.0)  # no leak, no mass: exactly 1 degF per 15-minute step at 4 degF/h
        # 6.5 degF = 6.5 steps = 97.5 min, times the 1.15 safety factor
        assert _lead(flat, 60.0, 66.5, cooling=False) == pytest.approx(97.5 * 1.15)

    def test_heat_and_cool_mirror(self):
        s = make_stepper(k=-0.06)
        heat = _lead(s, 60.0, 66.0, cooling=False, out=50.0)
        cool = _lead(s, 80.0, 74.0, cooling=True, out=90.0)  # the mirror image around 70
        assert heat == pytest.approx(cool)
        assert heat > 6.0 / 4.0 * 60.0  # the leak makes it longer than delta / gain

    def test_mass_makes_it_longer_than_the_first_order_formula(self):
        massless = make_stepper(k=-0.06, coupling=0.0)
        heavy = make_stepper(k=-0.06, coupling=0.8)
        first_order = leak_aware_minutes(60.0, 68.0, 4.0, -0.06, 50.0) * 1.15  # what the leak-aware formula says
        # Old expectation (the first-order formula) was wrong for a house with mass: it pulls the heat back out.
        assert _lead(massless, 60.0, 68.0, cooling=False) == pytest.approx(first_order, rel=0.08)
        assert _lead(heavy, 60.0, 68.0, cooling=False) > first_order * 1.15

    def test_afternoon_sun_helps_heating_and_hurts_cooling(self):
        dark, sunny = make_stepper(sun=0.0), make_stepper(sun=3.0)
        assert _lead(sunny, 60.0, 68.0, cooling=False) < _lead(dark, 60.0, 68.0, cooling=False)
        assert _lead(sunny, 80.0, 72.0, cooling=True, out=85.0) > _lead(dark, 80.0, 72.0, cooling=True, out=85.0)

    def test_night_has_no_sun_term(self):
        night = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)
        dark, sunny = make_stepper(sun=0.0), make_stepper(sun=3.0)
        assert _lead(sunny, 60.0, 68.0, cooling=False, at=night) == pytest.approx(
            _lead(dark, 60.0, 68.0, cooling=False, at=night)
        )

    def test_unreachable_target_is_the_ceiling(self):
        weak = make_stepper(k=-0.5, heat=1.0)  # 20 F outdoor: the house settles far below 72
        assert _lead(weak, 60.0, 72.0, cooling=False, out=20.0) == 240.0

    def test_safety_factor_floor_and_ceiling(self):
        flat = make_stepper(k=0.0)
        assert _lead(flat, 60.0, 61.0, cooling=False) == 30.0  # 15 min * 1.15 is under the floor
        assert _lead(flat, 60.0, 90.0, cooling=False) == 240.0  # 7.5 h of heat is over the ceiling
        assert _lead(flat, 60.0, 62.0, cooling=False, safety_multiplier=2.0, min_minutes=0.0) == pytest.approx(60.0)

    def test_already_there_is_the_floor(self):
        assert _lead(make_stepper(), 70.0, 68.0, cooling=False) == 30.0

    def test_no_gain_or_no_outdoor_cannot_be_computed(self):
        assert _lead(make_stepper(heat=0.0), 60.0, 68.0, cooling=False) is None
        assert (
            stepper_lead_minutes(
                make_stepper(),
                start_temp=60.0,
                target_temp=68.0,
                cooling=False,
                outdoor_at=lambda _w: None,
                start_ts=_AFTERNOON,
            )
            is None
        )

    def test_a_failing_model_never_raises(self):
        def boom(*_a):
            raise RuntimeError("bad fit")

        broken = HouseStepper(step=boom, heat_gain=4.0, cool_gain=4.0)
        assert _lead(broken, 60.0, 68.0, cooling=False) is None
        assert (
            stepper_lead_minutes_before(
                broken,
                event_ts=_AFTERNOON,
                start_temp=60.0,
                target_temp=68.0,
                cooling=False,
                outdoor_at=lambda _w: 50.0,
            )
            is None
        )

    def test_before_settles_the_start_on_the_lead(self):
        # sun only after noon: a run that must end at 17:00 starts late enough to meet some sun
        s = make_stepper(sun=3.0)
        end = datetime(2026, 7, 15, 17, 0, tzinfo=UTC)
        got = stepper_lead_minutes_before(
            s, event_ts=end, start_temp=60.0, target_temp=68.0, cooling=False, outdoor_at=lambda _w: 50.0
        )
        again = _lead(s, 60.0, 68.0, cooling=False, at=end - timedelta(minutes=got))
        assert got == pytest.approx(again, abs=2.0)


class TestComputeLeadUsesStepper:
    _STEP = make_stepper(k=0.0)  # 1 degF per 15 min

    def _kw(self, **over):
        base = {
            "start_temp": 60.0,
            "target_temp": 66.0,
            "outdoor_temp": 40.0,
            "k_passive": -0.03,
            "confidence": "medium",
            "house_stepper": self._STEP,
            "outdoor_at": lambda _w: 40.0,
            "event_ts": _START,
        }
        return {**base, **over}

    def test_stepper_wins_over_the_leak_formula(self):
        got = compute_lead_minutes_from_rate(6.0, 3.0, **_KW, **self._kw())
        assert got == pytest.approx(90.0 * 1.15)
        assert got != pytest.approx(compute_lead_minutes_from_rate(6.0, 3.0, **_KW, **self._kw(house_stepper=None)))

    def test_works_without_a_learned_rate(self):
        assert compute_lead_minutes_from_rate(6.0, None, **_KW, **self._kw()) == pytest.approx(90.0 * 1.15)

    def test_failure_falls_back_to_the_leak_formula(self):
        def boom(*_a):
            raise RuntimeError

        broken = HouseStepper(step=boom, heat_gain=4.0, cool_gain=4.0)
        leak = compute_lead_minutes_from_rate(6.0, 3.0, **_KW, **self._kw(house_stepper=None))
        assert compute_lead_minutes_from_rate(6.0, 3.0, **_KW, **self._kw(house_stepper=broken)) == pytest.approx(leak)

    def test_missing_outdoor_forecast_keeps_the_formula(self):
        plain = compute_lead_minutes_from_rate(6.0, 3.0, **_KW)
        got = compute_lead_minutes_from_rate(
            6.0,
            3.0,
            **_KW,
            start_temp=60.0,
            target_temp=66.0,
            house_stepper=self._STEP,
            event_ts=_START,
            confidence="high",
        )
        assert got == pytest.approx(plain)


def _lead_of(res) -> float:
    return (_START - res.precondition_start).total_seconds() / 60.0


class TestSchedulerConsumers:
    _STEP = make_stepper(k=0.0)

    def test_tou_phase_uses_the_stepper_when_supplied(self):
        model = {"k_active_heat": 3.0, "k_passive": -0.03, "confidence_k_passive": "medium"}
        with_s = resolve_tou_phase(
            [_SCHED], _NOW, 70.0, "heat", model, _CFG, outdoor_at=lambda _w: 40.0, house_stepper=self._STEP
        )
        without = resolve_tou_phase([_SCHED], _NOW, 70.0, "heat", model, _CFG, outdoor_at=lambda _w: 40.0)
        assert _lead_of(with_s) == pytest.approx(90.0 * 1.15)  # 70 -> 76 at 1 F per step
        assert _lead_of(without) == pytest.approx(
            compute_lead_minutes_from_rate(
                6.0,
                3.0,
                **_KW,
                start_temp=70.0,
                target_temp=76.0,
                outdoor_temp=40.0,
                k_passive=-0.03,
                confidence="medium",
            )
        )

    def test_tou_phase_cool_mirror(self):
        res = resolve_tou_phase(
            [_SCHED],
            _NOW,
            76.0,
            "cool",
            {"k_active_cool": -4.0, "k_passive": -0.02, "confidence_k_passive": "medium"},
            _CFG,
            outdoor_at=lambda _w: 95.0,
            house_stepper=self._STEP,
        )
        # 76 -> 68 (sleep/comfort heat edge) at 1 F per step = 8 steps = 120 min raw
        assert _lead_of(res) == pytest.approx(120.0 * 1.15)

    def test_tou_phase_without_a_stepper_is_unchanged(self):
        model = {"k_active_heat": 3.0}
        res = resolve_tou_phase([_SCHED], _NOW, 68.0, "heat", model, _CFG, outdoor_at=lambda _w: 40.0)
        assert _lead_of(res) == pytest.approx(compute_lead_minutes_from_rate(8.0, 3.0, **_KW))

    def test_away_vacation_uses_the_stepper_when_supplied(self):
        model = {"k_passive": -0.1, "confidence_k_passive": "high", "k_active_cool": -3.0, "k_active_heat": 3.0}
        args = ([_SCHED], _NOW, 79.0, 70.0, "cool", model, {**_CFG, "setback_heat": 63.0}, "away")
        with_s = resolve_tou_away_vacation_phase(*args, outdoor_at=lambda _w: 90.0, house_stepper=self._STEP)
        without = resolve_tou_away_vacation_phase(*args, outdoor_at=lambda _w: 90.0)
        depth = abs(with_s.target - 80.0)
        # 4 degF/h = 1 F per 15-minute step: depth F is depth * 15 minutes, times the 1.15 safety factor
        assert _lead_of(with_s) == pytest.approx(max(30.0, min(240.0, depth * 15.0 * 1.15)))
        assert _lead_of(with_s) != pytest.approx(_lead_of(without))

    def test_away_vacation_stepper_falls_back_to_the_current_outdoor(self):
        model = {"k_passive": -0.1, "confidence_k_passive": "high", "k_active_cool": -3.0}
        args = ([_SCHED], _NOW, 79.0, 90.0, "cool", model, {**_CFG, "setback_heat": 63.0}, "away")
        seen = []

        def stepper_step(t, mass, out, win, fan, hour):
            seen.append(out)
            return t, mass

        res = resolve_tou_away_vacation_phase(
            *args, outdoor_at=lambda _w: None, house_stepper=HouseStepper(stepper_step, 4.0, 4.0)
        )
        assert res.precondition_start is not None
        assert seen and set(seen) == {90.0}


class TestOdeCeilingGuardConsumer:
    @staticmethod
    def _inputs(**over):
        base = {
            "predicted_indoor": [{"ts": "2026-07-15T17:00:00+00:00", "temp": 90.0}],
            "hvac_mode": "off",
            "k_passive": -0.05,
            "confidence_k_passive": "medium",
            "k_passive_via_bridge": False,
            "k_active_cool": -2.0,
            "comfort_cool": 76.0,
            "outdoor": 85.0,
            "indoor": 72.0,
            "natural_vent_active": False,
            "ceiling_threshold": 76.0,
            "now": datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        }
        return OdeCeilingGuardInputs(**{**base, **over})

    def test_lead_comes_from_the_stepper_when_supplied(self):
        s = make_stepper(k=0.0)  # 76 -> 72 at 1 F per step = 4 steps = 60 min
        with_s = decide_ode_ceiling_guard(self._inputs(house_stepper=s)).lead_min
        without = decide_ode_ceiling_guard(self._inputs()).lead_min
        assert with_s == pytest.approx(60.0 * 1.15)
        assert without == pytest.approx(
            compute_lead_minutes_from_rate(
                4.0,
                -2.0,
                **_KW,
                start_temp=76.0,
                target_temp=72.0,
                outdoor_temp=85.0,
                k_passive=-0.05,
                cooling=True,
                confidence="medium",
            )
        )
        assert with_s != pytest.approx(without)

    def test_a_longer_house_lead_escalates_where_the_formula_would_wait(self):
        heavy = make_stepper(k=-0.06, coupling=0.8, cool=1.5)
        formula = decide_ode_ceiling_guard(self._inputs()).lead_min
        stepped = decide_ode_ceiling_guard(self._inputs(house_stepper=heavy)).lead_min
        assert stepped > formula


class TestNatVentPlanConsumer:
    _NOW = datetime(2026, 7, 15, 12, 0)

    def _curves(self):
        indoor = [
            {"ts": (self._NOW + timedelta(hours=h)).isoformat(), "temp": t}
            for h, t in ((0, 72.0), (1, 74.0), (4, 78.0), (5, 79.0))
        ]
        outdoor = [{"ts": (self._NOW + timedelta(hours=h)).isoformat(), "temp": 90.0} for h in range(6)]
        return indoor, outdoor

    @staticmethod
    def _gap(plan):
        return (plan["ceiling_breach_time"] - plan["precool_start_time"]).total_seconds() / 60

    def test_precool_lead_is_stepped_when_a_stepper_is_supplied(self):
        indoor, outdoor = self._curves()
        plan = compute_nat_vent_plan(
            indoor,
            outdoor,
            76.0,
            k_active_cool=-2.0,
            k_passive=-0.05,
            house_stepper=make_stepper(k=0.0),
            confidence_k_passive="medium",
        )
        assert self._gap(plan) == pytest.approx(60.0 * 1.15)  # 76 -> 72 at 1 F per step

    def test_without_a_stepper_the_leak_aware_lead_stands(self):
        indoor, outdoor = self._curves()
        plan = compute_nat_vent_plan(
            indoor, outdoor, 76.0, k_active_cool=-2.0, k_passive=-0.05, confidence_k_passive="medium"
        )
        expect = compute_lead_minutes_from_rate(
            4.0,
            -2.0,
            **_KW,
            start_temp=76.0,
            target_temp=72.0,
            outdoor_temp=90.0,
            k_passive=-0.05,
            cooling=True,
            confidence="medium",
        )
        assert self._gap(plan) == pytest.approx(expect)


class TestCoordinatorSuppliesTheStepper:
    def test_tou_resolution_passes_control_stepper(self):
        from tests.test_tou_away_vacation_coordinator import _build_coordinator

        sentinel = object()
        for occupancy, name in (("home", "resolve_tou_phase"), ("away", "resolve_tou_away_vacation_phase")):
            coord = _build_coordinator(occupancy)
            now = datetime(2026, 1, 5, 15, 30)
            with (
                patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=now),
                patch("custom_components.climate_advisor.coordinator.prediction_engines.control_stepper") as cs,
                patch(f"custom_components.climate_advisor.coordinator.{name}") as resolver,
            ):
                cs.return_value = sentinel
                resolver.return_value = None
                coord._resolve_tou_schedule_state()
            assert resolver.call_args.kwargs["house_stepper"] is sentinel, name
            cs.assert_called_with(coord, coord.config)

    def test_v3_zone_supplies_none(self):
        from tests.test_tou_away_vacation_coordinator import _build_coordinator

        coord = _build_coordinator("home")
        coord.config = {**coord.config, "primary_engine": "v3"}
        assert coord.control_house_stepper() is None

    def test_engine_reads_the_installed_provider(self):
        engine = object.__new__(AutomationEngine)
        assert engine._control_stepper() is None  # bare engine: no provider
        sentinel = object()
        engine._control_stepper_provider = MagicMock(return_value=sentinel)
        assert engine._control_stepper() is sentinel
        engine._control_stepper_provider = MagicMock(side_effect=RuntimeError)
        assert engine._control_stepper() is None  # a failing provider is "no model", never a crash
