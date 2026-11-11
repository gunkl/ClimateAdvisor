"""Issue #1517: the leak term and the house-model stepper are used only on a model graded medium or high.

Occupant view: a noisy or still-weak leak rate must never stretch the time-of-use bank, the ceiling guard's lead or the
nat-vent pre-cool to the 240-minute cap on a mild day (verified case: TOU cool 72 -> 68 at 80 F, 3 F/h, k -0.2 gave 240
minutes against 104 before). Below "medium" every consumer reads exactly today's plain ``delta / rate`` formula.
The one rule lives in ``compute_lead_minutes_from_rate``; each consumer passes the decisions' own confidence in.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor import coordinator as coord_mod
from custom_components.climate_advisor.classification_fsm import (
    ClassificationFsmEvent,
    ClassificationFsmEventKind,
    ClassificationFsmInputs,
    transition,
)
from custom_components.climate_advisor.nat_vent_plan import compute_nat_vent_plan
from custom_components.climate_advisor.ode_ceiling_guard import OdeCeilingGuardInputs, decide_ode_ceiling_guard
from custom_components.climate_advisor.scheduler import (
    COST_TAG_HIGH,
    Schedule,
    resolve_tou_away_vacation_phase,
    resolve_tou_phase,
)
from custom_components.climate_advisor.thermal_lead_time import compute_lead_minutes_from_rate, leak_aware_minutes
from tests.test_stepper_lead_1517 import make_stepper

_KW = {"min_minutes": 30.0, "max_minutes": 240.0, "safety_multiplier": 1.3, "fallback_minutes": 45.0}
_CFG = {"comfort_heat": 68.0, "comfort_cool": 76.0, "sleep_heat": 64.0, "sleep_cool": 72.0, "setback_cool": 80.0}
_START = datetime(2026, 1, 5, 16, 0, tzinfo=UTC)
_NOW = datetime(2026, 1, 5, 13, 0, tzinfo=UTC)
_SCHED = Schedule(id="s1", name="s", days=("all",), start="16:00", end="20:00", cost_tag=COST_TAG_HIGH)
_STEP = make_stepper(k=0.0)  # 1 degF per 15 minutes, no leak
_WEAK = ("none", "low", None)
_TRUSTED = ("medium", "high")
# 76 -> 72 cooling at 3 F/h with k -0.2 and 80 F outdoor: 1.15 x the first-order leak solution (plain formula: 104 min)
_LEAK_COOL_76_72 = leak_aware_minutes(76.0, 72.0, 3.0, -0.2, 80.0, cooling=True) * 1.15


def _lead_of(res) -> float:
    return (_START - res.precondition_start).total_seconds() / 60.0


class TestSharedRule:
    def _kw(self, conf, **over):
        base = {
            "start_temp": 72.0,
            "target_temp": 68.0,
            "outdoor_temp": 80.0,
            "k_passive": -0.2,
            "cooling": True,
            "confidence": conf,
        }
        return {**base, **over}

    @pytest.mark.parametrize("conf", _WEAK)
    def test_weak_confidence_is_the_plain_formula(self, conf):
        plain = compute_lead_minutes_from_rate(4.0, -3.0, **_KW)
        assert compute_lead_minutes_from_rate(4.0, -3.0, **_KW, **self._kw(conf)) == pytest.approx(plain)
        assert plain == pytest.approx(104.0)  # not the 240-minute cap the leak term gave on this mild day

    @pytest.mark.parametrize("conf", _TRUSTED)
    def test_medium_or_high_uses_the_leak(self, conf):
        got = compute_lead_minutes_from_rate(4.0, -3.0, **_KW, **self._kw(conf))
        assert got == 240.0  # the noisy-rate case the gate exists for: only a trusted rate may do this

    @pytest.mark.parametrize("conf", _WEAK)
    def test_weak_confidence_does_not_run_the_stepper(self, conf):
        kw = self._kw(conf, house_stepper=_STEP, outdoor_at=lambda _w: 80.0, event_ts=_START, k_passive=None)
        assert compute_lead_minutes_from_rate(4.0, -3.0, **_KW, **kw) == pytest.approx(104.0)

    @pytest.mark.parametrize("conf", _TRUSTED)
    def test_trusted_confidence_runs_the_stepper(self, conf):
        kw = self._kw(conf, house_stepper=_STEP, outdoor_at=lambda _w: 80.0, event_ts=_START)
        assert compute_lead_minutes_from_rate(4.0, -3.0, **_KW, **kw) == pytest.approx(60.0 * 1.15)


class TestTouHome:
    def _lead(self, conf, stepper=None):
        model = {"k_active_cool": -3.0, "k_passive": -0.2}
        if conf is not None:
            model["confidence_k_passive"] = conf
        res = resolve_tou_phase(
            [_SCHED], _NOW, 72.0, "cool", model, _CFG, outdoor_at=lambda _w: 80.0, house_stepper=stepper
        )
        return _lead_of(res)

    @pytest.mark.parametrize("conf", _WEAK)
    def test_weak_is_plain_even_with_a_stepper(self, conf):
        assert self._lead(conf, _STEP) == pytest.approx(compute_lead_minutes_from_rate(4.0, -3.0, **_KW))

    def test_the_old_confidence_key_is_the_fallback(self):
        model = {"k_active_cool": -3.0, "k_passive": -0.2, "confidence": "high"}
        res = resolve_tou_phase([_SCHED], _NOW, 72.0, "cool", model, _CFG, outdoor_at=lambda _w: 80.0)
        assert _lead_of(res) == 240.0

    @pytest.mark.parametrize("conf", _TRUSTED)
    def test_trusted_leak_and_stepper(self, conf):
        assert self._lead(conf) == 240.0
        assert self._lead(conf, _STEP) == pytest.approx(60.0 * 1.15)


class TestTouAwayVacation:
    def _lead(self, conf, stepper=None, mode="cool"):
        model = {"k_passive": -0.2, "k_active_cool": -3.0, "k_active_heat": 3.0}
        if conf is not None:
            model["confidence_k_passive"] = conf
        res = resolve_tou_away_vacation_phase(
            [_SCHED],
            _NOW,
            79.0,
            90.0,
            mode,
            model,
            {**_CFG, "setback_heat": 63.0},
            "away",
            outdoor_at=lambda _w: 90.0,
            house_stepper=stepper,
        )
        return _lead_of(res), res

    @pytest.mark.parametrize("conf", _WEAK)
    @pytest.mark.parametrize("mode", ["cool", "heat"])
    def test_weak_is_plain_even_with_a_stepper(self, conf, mode):
        lead, res = self._lead(conf, _STEP, mode)
        plain = compute_lead_minutes_from_rate(
            abs(res.target - (80.0 if mode == "cool" else 63.0)),
            -3.0 if mode == "cool" else 3.0,
            **{**_KW, "safety_multiplier": 1.3},
        )
        assert lead == pytest.approx(max(30.0, min(240.0, plain)), abs=1.0)

    @pytest.mark.parametrize("conf", _TRUSTED)
    def test_trusted_steps_the_model(self, conf):
        lead, res = self._lead(conf, _STEP)
        depth = abs(res.target - 80.0)
        assert lead == pytest.approx(max(30.0, min(240.0, depth * 15.0 * 1.15)))


class TestOdeCeilingGuard:
    @staticmethod
    def _inputs(conf, **over):
        base = {
            "predicted_indoor": [{"ts": "2026-07-15T17:00:00+00:00", "timestamp": None, "temp": 90.0}],
            "hvac_mode": "off",
            "k_passive": -0.2,
            "confidence_k_passive": conf,
            "k_passive_via_bridge": True,  # lets the guard evaluate when the grade is "low"
            "k_active_cool": -3.0,
            "comfort_cool": 76.0,
            "outdoor": 80.0,
            "indoor": 72.0,
            "natural_vent_active": False,
            "ceiling_threshold": 76.0,
            "now": datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        }
        base["predicted_indoor"] = [{"ts": "2026-07-15T17:00:00+00:00", "temp": 90.0}]
        return OdeCeilingGuardInputs(**{**base, **over})

    @pytest.mark.parametrize("conf", ["none", "low"])
    def test_weak_is_plain_with_or_without_a_stepper(self, conf):
        for stepper in (None, _STEP):
            got = decide_ode_ceiling_guard(self._inputs(conf, house_stepper=stepper)).lead_min
            assert got == pytest.approx(104.0)  # 4 F / 3 F/h x 60 x 1.3, no leak, no stepper

    @pytest.mark.parametrize("conf", _TRUSTED)
    def test_trusted_uses_leak_and_stepper(self, conf):
        assert decide_ode_ceiling_guard(self._inputs(conf)).lead_min == pytest.approx(_LEAK_COOL_76_72)
        assert decide_ode_ceiling_guard(self._inputs(conf, house_stepper=_STEP)).lead_min == pytest.approx(60.0 * 1.15)


class TestNatVentPlan:
    _T0 = datetime(2026, 7, 15, 12, 0)

    def _gap(self, conf, stepper=None):
        indoor = [
            {"ts": (self._T0 + timedelta(hours=h)).isoformat(), "temp": t}
            for h, t in ((0, 72.0), (1, 74.0), (4, 78.0), (5, 79.0))
        ]
        outdoor = [{"ts": (self._T0 + timedelta(hours=h)).isoformat(), "temp": 80.0} for h in range(6)]
        plan = compute_nat_vent_plan(
            indoor,
            outdoor,
            76.0,
            k_active_cool=-3.0,
            k_passive=-0.2,
            house_stepper=stepper,
            confidence_k_passive=conf,
        )
        return (plan["ceiling_breach_time"] - plan["precool_start_time"]).total_seconds() / 60

    @pytest.mark.parametrize("conf", _WEAK)
    def test_weak_is_plain_with_or_without_a_stepper(self, conf):
        plain = compute_lead_minutes_from_rate(4.0, -3.0, **_KW)
        assert self._gap(conf) == pytest.approx(plain)
        assert self._gap(conf, _STEP) == pytest.approx(plain)

    @pytest.mark.parametrize("conf", _TRUSTED)
    def test_trusted_uses_leak_and_stepper(self, conf):
        assert self._gap(conf) == pytest.approx(_LEAK_COOL_76_72)
        assert self._gap(conf, _STEP) == pytest.approx(60.0 * 1.15)


class TestCoordinatorPassesTheDecisionsConfidence:
    def test_plan_gets_the_grade_the_decisions_see(self):
        stub = MagicMock()
        stub._current_classification = MagicMock(day_type="hot", window_opportunity_morning_start=None)
        stub.control_predicted_indoor.return_value = [{"ts": "2026-07-13T14:00:00+00:00", "temp": 75.0}]
        stub.control_house_stepper.return_value = None
        stub.control_thermal_model.return_value = {
            "k_active_cool": -3.2,
            "k_passive": -0.05,
            "confidence_k_passive": "low",
        }
        stub._thermal_model = {}
        stub._hourly_forecast_temps = []
        stub.config = {"comfort_cool": 76, "comfort_heat": 68}
        stub._stabilize_nat_vent_cutoff_reason = lambda plan: plan
        with (
            patch.object(coord_mod, "compute_nat_vent_plan", return_value={}) as plan,
            patch.object(coord_mod, "_build_future_forecast_outdoor", return_value=[]),
        ):
            coord_mod.ClimateAdvisorCoordinator._compute_and_cache_nat_vent_plan(stub)
        assert plan.call_args.kwargs["confidence_k_passive"] == "low"


class TestHouseStepperReachesTheGuard:
    """Issue #1517 follow-up: the stepper must travel provider -> FSM inputs -> guard, and into the chart's walk."""

    def test_fsm_inputs_carry_it_to_the_guard(self):
        inputs = ClassificationFsmInputs(
            occupancy_mode="home",
            manual_override_active=False,
            paused_by_door=False,
            natural_vent_active=False,
            whf_owns_hvac=False,
            aggressive_savings=False,
            fan_mode="disabled",
            predicted_indoor=[{"ts": "2026-07-15T17:00:00+00:00", "temp": 90.0}],
            hvac_mode="off",
            k_passive=-0.2,
            confidence_k_passive="high",
            k_passive_via_bridge=False,
            k_active_cool=-3.0,
            comfort_cool=76.0,
            outdoor=80.0,
            indoor=72.0,
            ceiling_threshold=76.0,
            now=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
            house_stepper=_STEP,
        )
        decision = transition(ClassificationFsmEvent(kind=ClassificationFsmEventKind.CYCLE_EVALUATED, inputs=inputs))
        assert decision.ceiling_decision is not None
        assert decision.ceiling_decision.lead_min == pytest.approx(60.0 * 1.15)

    def test_automation_provider_reaches_the_fsm_inputs(self):
        from tests.test_savings_ceiling_guard_1093 import _classification, _make_engine

        engine = _make_engine(savings=False)
        engine.inputs.thermal = {"k_passive": -0.2, "confidence_k_passive": "high", "k_active_cool": -3.0}
        engine._control_stepper_provider = lambda: _STEP
        curve = [{"ts": "2026-07-15T17:00:00+00:00", "temp": 90.0}]
        seen = []
        real = coord_mod  # noqa: F841 - keep the module imported for the patch target below
        with patch("custom_components.climate_advisor.automation._classification_fsm_transition") as fsm:
            fsm.side_effect = lambda event: seen.append(event.inputs.house_stepper) or MagicMock()
            engine._resolve_classification_fsm_state(classification=_classification(), predicted_indoor=curve)
        assert seen == [_STEP]

    def test_walk_forward_regime_hands_it_to_the_guard(self):
        ts1, ts2 = "2026-07-13T12:00:00", "2026-07-13T13:00:00"
        band = [{"ts": t, "lower": 68.0, "upper": 76.0} for t in (ts1, ts2)]
        indoor = [{"ts": ts1, "temp": 74.0}, {"ts": ts2, "temp": 79.0}]
        outdoor = [{"ts": t, "temp": 90.0} for t in (ts1, ts2)]
        config = {
            "comfort_heat": 68.0, "comfort_cool": 76.0, "sleep_heat": 64.0, "sleep_cool": 72.0,
            "wake_time": "06:00:00", "sleep_time": "22:00:00", "fan_mode": "hvac_fan",
            "natural_vent_delta": 3.0, "nat_vent_hysteresis_f": 1.0, "aggressive_savings": False,
        }  # fmt: skip
        model = {"k_passive": -0.05, "confidence_k_passive": "high", "k_active_cool": -2.0}
        with patch.object(coord_mod, "decide_ode_ceiling_guard") as guard:
            guard.return_value = MagicMock(outcome="no_breach_predicted")
            coord_mod._walk_forward_regime(
                {date(2026, 7, 13): "off"}, indoor, outdoor, band, config, "home", model, False, None, 76.0, False,
                house_stepper=_STEP,
            )  # fmt: skip
        assert guard.called
        assert all(c.args[0].house_stepper is _STEP for c in guard.call_args_list)
