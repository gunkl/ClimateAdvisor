"""Issue #1517: every consumer that turns a rate into a lead time uses the leak-aware lead.

Occupant view: pre-heat, pre-cool and the TOU bank start early enough to reach the target because the lead allows
for the house drifting toward the outdoor temperature while the equipment runs. The shared function is covered in
test_thermal_lead_time.py; here each consumer is shown to pass the start/target/outdoor/k_passive inputs, and to
keep the plain formula when it has no outdoor or no k_passive.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.climate_advisor.nat_vent_plan import compute_nat_vent_plan
from custom_components.climate_advisor.ode_ceiling_guard import (
    OdeCeilingGuardInputs,
    decide_ode_ceiling_guard,
)
from custom_components.climate_advisor.scheduler import (
    COST_TAG_HIGH,
    Schedule,
    resolve_tou_away_vacation_phase,
    resolve_tou_phase,
)
from custom_components.climate_advisor.thermal_lead_time import compute_lead_minutes_from_rate

_KW = {"min_minutes": 30.0, "max_minutes": 240.0, "safety_multiplier": 1.3, "fallback_minutes": 45.0}
_CFG = {"comfort_heat": 68.0, "comfort_cool": 76.0, "sleep_heat": 64.0, "sleep_cool": 72.0, "setback_cool": 80.0}
_START = datetime(2026, 1, 5, 16, 0, tzinfo=UTC)
_NOW = datetime(2026, 1, 5, 13, 0, tzinfo=UTC)
_SCHED = Schedule(id="s1", name="s", days=("all",), start="16:00", end="20:00", cost_tag=COST_TAG_HIGH)


def _lead_of(res) -> float:
    return (_START - res.precondition_start).total_seconds() / 60.0


class TestResolveTouPhase:
    def test_heat_bank_uses_the_leak_at_the_schedule_start(self):
        model = {"k_active_heat": 3.0, "k_passive": -0.03}
        seen = []

        def outdoor_at(when):
            seen.append(when)
            return 40.0

        res = resolve_tou_phase([_SCHED], _NOW, 70.0, "heat", model, _CFG, outdoor_at=outdoor_at)
        assert seen == [_START]  # the forecast is asked for at the period start
        # target is comfort_cool 76, indoor 70 -> 6 F
        expect = compute_lead_minutes_from_rate(
            6.0, 3.0, **_KW, start_temp=70.0, target_temp=76.0, outdoor_temp=40.0, k_passive=-0.03
        )
        assert _lead_of(res) == pytest.approx(expect)
        assert expect > compute_lead_minutes_from_rate(6.0, 3.0, **_KW)  # cold outdoor: starts earlier than plain

    def test_cool_bank_uses_the_leak(self):
        model = {"k_active_cool": -4.0, "k_passive": -0.02}
        res = resolve_tou_phase([_SCHED], _NOW, 76.0, "cool", model, _CFG, outdoor_at=lambda _w: 95.0)
        expect = compute_lead_minutes_from_rate(
            8.0, -4.0, **_KW, start_temp=76.0, target_temp=68.0, outdoor_temp=95.0, k_passive=-0.02, cooling=True
        )
        assert _lead_of(res) == pytest.approx(expect)

    def test_without_outdoor_or_k_passive_it_is_the_plain_formula(self):
        plain = compute_lead_minutes_from_rate(8.0, 3.0, **_KW)
        no_outdoor = resolve_tou_phase([_SCHED], _NOW, 68.0, "heat", {"k_active_heat": 3.0, "k_passive": -0.03}, _CFG)
        no_k = resolve_tou_phase([_SCHED], _NOW, 68.0, "heat", {"k_active_heat": 3.0}, _CFG, outdoor_at=lambda _w: 40.0)
        assert _lead_of(no_outdoor) == pytest.approx(plain)
        assert _lead_of(no_k) == pytest.approx(plain)


class TestAwayVacationPhase:
    _MODEL = {"k_passive": -0.1, "confidence_k_passive": "high", "k_active_cool": -3.0, "k_active_heat": 3.0}

    def _resolve(self, mode, outdoor_now, **kw):
        return resolve_tou_away_vacation_phase(
            [_SCHED], _NOW, 79.0, outdoor_now, mode, self._MODEL, {**_CFG, "setback_heat": 63.0}, "away", **kw
        )

    def test_cool_bank_uses_the_forecast_outdoor_at_the_start(self):
        res = self._resolve("cool", 70.0, outdoor_at=lambda _w: 90.0)
        edge, depth = 80.0, abs(res.target - 80.0)
        expect = compute_lead_minutes_from_rate(
            depth, -3.0, **_KW, start_temp=edge, target_temp=res.target, outdoor_temp=90.0, k_passive=-0.1, cooling=True
        )
        assert _lead_of(res) == pytest.approx(expect)

    def test_falls_back_to_the_current_outdoor_when_no_forecast(self):
        with_current = self._resolve("cool", 90.0)
        with_forecast = self._resolve("cool", 70.0, outdoor_at=lambda _w: 90.0)
        assert _lead_of(with_current) == pytest.approx(_lead_of(with_forecast))

    def test_heat_mirror_uses_the_leak(self):
        res = self._resolve("heat", 50.0, outdoor_at=lambda _w: 50.0)
        edge = 63.0
        expect = compute_lead_minutes_from_rate(
            abs(res.target - edge),
            3.0,
            **_KW,
            start_temp=edge,
            target_temp=res.target,
            outdoor_temp=50.0,
            k_passive=-0.1,
        )
        assert _lead_of(res) == pytest.approx(expect)


def _guard_inputs(**over):
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


class TestOdeCeilingGuard:
    def test_lead_is_leak_aware(self):
        d = decide_ode_ceiling_guard(_guard_inputs())
        expect = compute_lead_minutes_from_rate(
            4.0, -2.0, **_KW, start_temp=76.0, target_temp=72.0, outdoor_temp=85.0, k_passive=-0.05, cooling=True
        )
        assert d.lead_min == pytest.approx(expect)
        assert d.lead_min != pytest.approx(4.0 / 2.0 * 60 * 1.3)

    def test_hotter_outdoor_starts_earlier(self):
        mild = decide_ode_ceiling_guard(_guard_inputs(outdoor=80.0)).lead_min
        hot = decide_ode_ceiling_guard(_guard_inputs(outdoor=100.0)).lead_min
        assert hot > mild


class TestNatVentPlan:
    _NOW = datetime(2026, 7, 15, 12, 0)

    def _curves(self, outdoor_at_breach):
        indoor = [
            {"ts": (self._NOW + timedelta(hours=h)).isoformat(), "temp": t}
            for h, t in ((0, 72.0), (1, 74.0), (4, 78.0), (5, 79.0))
        ]
        outdoor = [{"ts": (self._NOW + timedelta(hours=h)).isoformat(), "temp": 80.0} for h in (0, 1, 2, 3)]
        outdoor.append({"ts": (self._NOW + timedelta(hours=4)).isoformat(), "temp": outdoor_at_breach})
        return indoor, outdoor

    def test_precool_lead_is_leak_aware_and_uses_the_outdoor_at_the_breach(self):
        indoor, outdoor = self._curves(95.0)
        plan = compute_nat_vent_plan(indoor, outdoor, 76.0, k_active_cool=-2.0, k_passive=-0.05)
        expect = compute_lead_minutes_from_rate(
            4.0, -2.0, **_KW, start_temp=76.0, target_temp=72.0, outdoor_temp=95.0, k_passive=-0.05, cooling=True
        )
        assert (plan["ceiling_breach_time"] - plan["precool_start_time"]).total_seconds() / 60 == pytest.approx(expect)

    def test_without_k_passive_it_is_the_plain_lead(self):
        indoor, outdoor = self._curves(95.0)
        plan = compute_nat_vent_plan(indoor, outdoor, 76.0, k_active_cool=-2.0)
        gap = (plan["ceiling_breach_time"] - plan["precool_start_time"]).total_seconds() / 60
        assert gap == pytest.approx(4.0 / 2.0 * 60 * 1.3)
