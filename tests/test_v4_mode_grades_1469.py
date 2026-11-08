"""Issue #1469: v4 grades heating and cooling apart, for the Debug tab only.

Occupant view: v4 read "low confidence" all winter while it predicted heating well (Zone 1, 2026-11-08: heating 45
usable starts, cooling 0 of the 5 needed), and nothing said that heating itself was fine. Now each mode has its own
grade, shown as "heating ok · cooling low". Display only, by the owner's decision: the decisions keep reading the
whole-model grade, so nothing the house does changes.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from custom_components.climate_advisor import predictor_v4 as p4
from custom_components.climate_advisor import v4_shadow

from .test_predictor_v4 import HALL, T0, TZ, simulate


def _mixed(days: int = 26):
    samples, _truth, _ = simulate(
        T0 + timedelta(days=90), days, base=lambda d: 40.0 + 2.2 * d, amp=12.0, heat=True, cool=True,
        heat_day=66.0, cool_sp=74.0, noise=0.03,
    )  # fmt: skip
    return samples


@pytest.fixture(scope="module")
def heat_only():
    samples, _truth, _ = simulate(T0, 16, noise=0.03)
    return p4.fit(samples[: 12 * 96], primary=HALL, sensors=[HALL], tz=TZ, now=samples[12 * 96].ts)


@pytest.fixture(scope="module")
def heat_starts_poisoned():
    """Both modes used, but no heating start is usable (the setpoint moves on every start sample)."""
    out, prev = [], False
    for sm in _mixed():
        heating = (sm.heat_duty or 0) > 0
        if heating and not prev and sm.heat_sp_f is not None:
            sm = dataclasses.replace(sm, heat_sp_f=sm.heat_sp_f + 0.5)
        prev = heating
        out.append(sm)
    return p4.fit(out, primary=HALL, sensors=[HALL], tz=TZ, now=out[-1].ts)


class TestModeGrades:
    def test_home_without_ac_shows_heating_ok(self, heat_only):
        """The whole-model grade stays low (cooling unseen) but heating, the mode the home uses, is ok."""
        assert heat_only.confidence == "low" and heat_only.fit["reason"] == "heating or cooling not yet observed"
        modes = heat_only.fit["modes"]
        assert modes["heat"]["grade"] == "ok", modes["heat"]
        assert modes["heat"]["n"] >= p4.GATE_MIN_ANCHORS
        assert modes["heat"]["mae"] <= (1.0 - p4.GATE_MIN_SKILL) * modes["heat"]["flat"]
        assert modes["cool"]["grade"] == "unseen"

    def test_a_mode_without_usable_starts_stays_low_while_the_other_is_ok(self, heat_starts_poisoned):
        m = heat_starts_poisoned
        assert m.confidence == "low"
        assert m.fit["modes"]["heat"]["grade"] == "low"
        assert "starts" in m.fit["modes"]["heat"]["reason"]
        assert m.fit["modes"]["cool"]["grade"] == "ok"

    def test_short_history_is_low_for_every_used_mode(self):
        samples, _t, _ = simulate(T0, 8, noise=0.03)
        m = p4.fit(samples[: 6 * 96], primary=HALL, sensors=[HALL], tz=TZ, now=samples[6 * 96].ts)
        assert m.confidence in ("low", "none")
        if m.confidence == "low":
            assert m.fit["modes"]["heat"]["grade"] == "low"
            assert m.fit["modes"]["heat"]["reason"] == "under 10 days of samples"
        else:
            assert m.fit["modes"]["heat"]["grade"] == "none"

    def test_round_trip_keeps_the_mode_grades(self, heat_only):
        back = p4.from_dict(p4.to_dict(heat_only))
        assert back is not None and back.fit["modes"] == heat_only.fit["modes"]


class TestNoBehaviourChange:
    """The per-mode grades never reach a decision: the control rates are the same with or without them."""

    def test_control_rates_ignore_the_mode_grades(self, heat_only):
        fit = {k: v for k, v in heat_only.fit.items() if k != "modes"}
        without = p4.from_dict({**p4.to_dict(heat_only), "fit": fit})
        assert without is not None
        assert p4.control_rates(heat_only) == p4.control_rates(without)
        rates = p4.control_rates(heat_only)
        assert rates["confidence"] == rates["confidence_k_passive"] == "low"  # the whole-model grade, as before
        assert not {"confidence_heat", "confidence_cool"} & set(rates)

    def test_whole_model_grade_is_unchanged(self, heat_only, heat_starts_poisoned):
        assert heat_only.confidence == "low" and heat_only.fit["reason"] == "heating or cooling not yet observed"
        assert heat_starts_poisoned.confidence == "low" and "accepted" in heat_starts_poisoned.fit["reason"]


class TestWords:
    def test_by_mode_names_each_grade(self, heat_only):
        s = v4_shadow.model_summary(p4.to_dict(heat_only))
        assert s["modes"]["heat"]["grade"] == "ok" and s["modes"]["cool"]["grade"] == "unseen"
        _why, by_mode = v4_shadow.describe_grade(s)
        assert by_mode.startswith("heating ok: ") and "cooling not seen" in by_mode

    def test_live_low_cooling_reads_its_reason(self):
        fit = {
            "span_days": 29.97, "sample_days": 29.97, "heat_seen": True, "cool_seen": True,
            "starts_accepted": {"heat": 45, "cool": 0}, "starts_total": {"heat": 61, "cool": 2},
            "modes": {"heat": {"grade": "ok", "reason": ""},
                      "cool": {"grade": "low", "reason": "too few accepted thermostat starts"}},
        }  # fmt: skip
        s = v4_shadow.model_summary({"confidence": "low", "fit": fit})
        _why, by_mode = v4_shadow.describe_grade(s)
        assert by_mode == (
            "heating ok: 45 usable of 61 starts (enough) · "
            "cooling low: 0 usable of 2 starts (needs 5) in the last 30 days"
        )
