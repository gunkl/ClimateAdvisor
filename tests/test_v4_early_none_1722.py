"""Issue #1722: a window under 3 days of samples returns the "none" model without running the search.

Occupant view: the night fit of a new install (or of a house whose history was just trimmed) used to spend up to 24 s
on x86 (about two minutes on a Raspberry Pi) in the Levenberg-Marquardt search, the thermostat calibration and the MAE
pass, then throw the result away: the confidence gate says "none" for under 3 days of samples whatever the search
finds. The early return skips that work and keeps everything a "none" model carries (learned forecast correction,
openness, thermostat starts, the Debug-tab fields and the #1525 carry source).

Nothing here mirrors the code under test. The "before" model is the real full path: ``_under_min_days_model`` is the
one seam the early return goes through, so patching it to ``None`` makes the same ``fit`` run the whole search.
"""

from __future__ import annotations

from datetime import date

import pytest

from custom_components.climate_advisor import night_replay as nr
from custom_components.climate_advisor import predictor_v4 as p4
from custom_components.climate_advisor import v4_shadow
from tests.test_night_strategy_replay import _history as replay_history
from tests.test_predictor_v4 import HALL, T0, TZ, simulate
from tests.timeline_fixtures import TZ as REPLAY_TZ

SAMPLES_PER_DAY = 96
UNDER = 2 * SAMPLES_PER_DAY  # 2 days: below GATE_MIN_DAYS_NONE
OVER = 7 * SAMPLES_PER_DAY // 2  # 3.5 days: above it

# Everything the search derives. These (and only these) may differ from the model the full path returned.
LM_DERIVED = {
    "/house/",  # the nine house parameters: defaults here, fitted there
    "/nodes/",  # extra-node parameters (none in these single-sensor fits; defaults when present)
    "/thermostat/calibrated",  # closed-loop calibration is part of the search
    "/thermostat/on_below_f",
    "/thermostat/off_above_f",
    "/thermostat/on_above_f",
    "/thermostat/off_below_f",
    "/fit/mae_by_lead",
    "/fit/flat_mae_by_lead",
    "/fit/per_sensor",
    "/fit/cost",
    "/fit/iterations",
    "/fit/converged",
    "/fit/restarts",
    "/fit/restart_won",
    "/fit/skill_vs_flat",
    "/fit/thermostat_calibration",
    "/fit/carried",  # carry records are derived from the fitted gains
    "/fit/carried_params",
    "/fit/node_choice/scores/mae",  # replay MAE of the models A / B: scored on the fitted (here default) parameters
}


def _flatten(obj, path=""):
    if isinstance(obj, dict) and obj:
        for k, v in obj.items():
            yield from _flatten(v, f"{path}/{k}")
    else:
        yield path, obj


def _differs(a: dict, b: dict) -> set[str]:
    fa, fb = dict(_flatten(a)), dict(_flatten(b))
    return {k for k in fa.keys() | fb.keys() if fa.get(k, "<absent>") != fb.get(k, "<absent>")}


def _allowed(path: str) -> bool:
    return any(path == a or (a.endswith("/") and path.startswith(a)) or path.startswith(a + "/") for a in LM_DERIVED)


@pytest.fixture(scope="module")
def history():
    smp, _t, _ = simulate(T0, 12, noise=0.03)
    return smp


def _fit(samples, **kw):
    return p4.fit(samples, primary=HALL, sensors=[HALL], tz=TZ, now=samples[-1].ts, **kw)


@pytest.fixture(scope="module")
def prev_model(history):
    prev = _fit(history[: 10 * SAMPLES_PER_DAY])
    assert prev.confidence != "none", prev.fit["reason"]  # a usable previous model: it is the carry source
    return prev


@pytest.fixture
def no_search(monkeypatch):
    """Any entry to the search (LM, thermostat calibration, scoring rollout) fails the test."""

    def boom(*_a, **_k):
        raise AssertionError("the search ran")

    monkeypatch.setattr(p4, "_fit_problem", boom)
    monkeypatch.setattr(p4, "_calibrate_thermostat", boom)
    monkeypatch.setattr(p4._Problem, "all_residuals", boom)
    monkeypatch.setattr(p4._Problem, "lead_errors", boom)


@pytest.fixture
def full_path(monkeypatch):
    """The pre-change behaviour: the same ``fit``, the early return switched off."""
    monkeypatch.setattr(p4, "_under_min_days_model", lambda *_a, **_k: None)


class TestUnderThreeDays:
    def test_none_without_running_the_search(self, history, no_search):
        m = _fit(history[:UNDER])
        assert m.confidence == "none"
        assert m.fit["reason"] == p4.REASON_UNDER_MIN_DAYS == "under 3 days of samples"
        assert m.fit["sample_days"] < p4.GATE_MIN_DAYS_NONE

    def test_debug_fields_say_what_the_house_did(self, history, no_search):
        m = _fit(history[:UNDER])
        assert m.fit["n_anchors"] >= p4.GATE_MIN_ANCHORS  # not "0 points"
        assert m.fit["heat_seen"] is True and m.fit["cool_seen"] is False  # a house that heated is not "not seen"
        assert m.fit["starts_total"]["heat"] > 0
        assert m.fit["span_days"] == pytest.approx(UNDER / SAMPLES_PER_DAY, abs=0.02)
        assert m.fit["modes"]["heat"] == {"grade": "none", "reason": "no usable fit"}
        assert m.fit["modes"]["cool"] == {"grade": "none", "reason": "no usable fit"}
        assert m.fit["frozen"] == ["h/a_f", "h/a_w", "h/q_c"]  # no data behind them (the Debug tab and night debug)
        assert p4.control_rates(m) == {}  # a "none" model exports no rate to the decisions

    def test_keeps_the_learned_forecast_openness_and_thermostat_starts(self, history, no_search):
        win = history[:UNDER]
        m = _fit(win)
        assert m.forecast == p4.learn_forecast_correction(win, tz=TZ)
        assert m.openness == p4.learn_openness(win, tz=TZ)
        th, _stats = p4.learn_thermostat_detail(win, primary=HALL)
        assert m.thermostat.burst_minutes == th.burst_minutes
        assert m.thermostat.reading_step_f == th.reading_step_f
        # the cooling mode was never seen: its offsets are the defaults, as on the full path
        assert (m.thermostat.on_above_f, m.thermostat.off_below_f) == (
            p4.THERMOSTAT_DEFAULT_ON_F,
            p4.THERMOSTAT_DEFAULT_OFF_F,
        )
        assert m.house == p4.HOUSE_START

    def test_the_carry_source_survives(self, history, prev_model, no_search):
        m = _fit(history[:UNDER], warm_start=prev_model)
        kept = m.fit["carry_source"]
        assert kept["confidence"] == prev_model.confidence != "none"
        assert kept["house"] == p4.to_dict(prev_model)["house"]

    def test_choose_control_model_never_runs_the_search(self, history, no_search):
        win = history[:UNDER]
        rows = [_row(s) for s in win]
        ctl = p4.choose_control_model(rows, tz=TZ, now=win[-1].ts, primary=HALL, sensors=[HALL])
        assert ctl.model.confidence == "none" and ctl.model.fit["reason"] == p4.REASON_UNDER_MIN_DAYS


class TestFullPathUnchangedAtThreeDays:
    def test_three_and_a_half_days_still_run_the_search(self, history, monkeypatch):
        calls = []
        real = p4._fit_problem
        monkeypatch.setattr(p4, "_fit_problem", lambda *a, **k: calls.append(a[6]) or real(*a, **k))
        m = _fit(history[:OVER])
        assert len(calls) == 1 and calls[0] >= p4.GATE_MIN_DAYS_NONE
        assert m.fit["iterations"] > 0 and m.fit["mae_by_lead"]
        assert m.fit["reason"] != p4.REASON_UNDER_MIN_DAYS

    def test_the_seam_declines_a_long_window(self, history):
        win = history[:OVER]
        prob = p4._Problem(win, primary=HALL, extra=[], tz=TZ, horizon_steps=48, max_anchors=160)
        th, tstats = p4.learn_thermostat_detail(win, primary=HALL)
        base = p4._default_model(HALL, [HALL], "t", th, p4.learn_forecast_correction(win, tz=TZ), {})
        assert p4._under_min_days_model(base, prob, tstats, p4.GATE_MIN_DAYS_NONE) is None


class TestWarmStartSkipsAnEarlyNoneModel:
    """An early "none" model holds the default house and nodes. Warm-starting from it is a cold start that also pays
    for a second start; the fit must take the cold-start path (one start when it converges)."""

    def test_after_an_early_none_model_the_fit_is_the_cold_start_fit(self, history):
        early_none = _fit(history[:UNDER])
        assert early_none.fit["reason"] == p4.REASON_UNDER_MIN_DAYS
        cold = _fit(history[:OVER])
        after_early_none = _fit(history[:OVER], warm_start=early_none)
        assert after_early_none.fit["iterations"] > 0  # the real search ran
        assert p4.to_dict(after_early_none) == p4.to_dict(cold)
        assert after_early_none.fit["restarts"] == cold.fit["restarts"]

    def test_a_usable_previous_model_still_warm_starts(self, history, prev_model):
        cold = _fit(history[:OVER])
        warm = _fit(history[:OVER], warm_start=prev_model)
        assert warm.fit["restarts"] == 1  # this warm start moved far (#1726), so it still gets the cold-defaults start
        assert p4.to_dict(warm) != p4.to_dict(cold)


class TestSameModelAsBeforeExceptTheSearch:
    def test_fit_differs_only_in_search_derived_fields(self, history, prev_model, monkeypatch):
        win = history[:UNDER]
        early = p4.to_dict(_fit(win, warm_start=prev_model))
        monkeypatch.setattr(p4, "_under_min_days_model", lambda *_a, **_k: None)
        before = p4.to_dict(_fit(win, warm_start=prev_model))
        assert before["fit"]["iterations"] > 0  # the reference really ran the search
        changed = _differs(early, before)
        assert changed, "the search-derived fields should differ"
        assert {p for p in changed if not _allowed(p)} == set(), sorted(p for p in changed if not _allowed(p))
        for must_match in ("/confidence", "/fit/reason", "/fit/n_anchors", "/fit/modes/heat/grade", "/fit/frozen"):
            assert must_match not in changed
        assert any(k.startswith("/fit/carry_source") for k in dict(_flatten(early)))
        assert not any(k.startswith("/fit/carry_source") for k in changed)

    def test_run_fit_differs_only_in_search_derived_fields(self, history, monkeypatch):
        win = history[:UNDER]
        rows = [_row(s) for s in win]
        kw = dict(chart_entries=rows, snapshots_fn=None, tz=TZ, now=win[-1].ts, primary=HALL, sensors=[HALL])
        early, _s, line = v4_shadow.run_fit(**kw)
        monkeypatch.setattr(p4, "_under_min_days_model", lambda *_a, **_k: None)
        before, _s2, _l2 = v4_shadow.run_fit(**kw)
        assert before["fit"]["iterations"] > 0
        assert early["fit"]["iterations"] == 0  # the early return, not the search
        assert early["confidence"] == "none" and early["fit"]["reason"] == p4.REASON_UNDER_MIN_DAYS
        changed = _differs(early, before)
        assert {p for p in changed if not _allowed(p)} == set(), sorted(p for p in changed if not _allowed(p))
        assert "reason='under 3 days of samples'" in line


class TestNightReplayEarlyWeeks:
    def test_weeks_with_under_three_days_of_training_skip_the_search(self, monkeypatch):
        # history from Saturday: the week of the second Monday trains on 2 days, the next on 9
        smp = replay_history(30)[5 * SAMPLES_PER_DAY :]
        seen: list[float] = []
        real = p4._fit_problem

        def spy(*a, **k):
            seen.append(a[6])  # sample_days
            return real(*a, **k)

        monkeypatch.setattr(p4, "_fit_problem", spy)
        rep = nr.run(smp, REPLAY_TZ, bed=(22, 30), wake=(6, 30), train_days=30, until=date(2026, 6, 16))
        assert rep["excluded"].get("model_none", 0) >= 1  # the thin-training week was found and skipped
        assert seen, "the nine-day week still fits"
        assert all(d >= p4.GATE_MIN_DAYS_NONE for d in seen), seen  # no search under 3 days
        assert len(seen) == 1


def _row(s: p4.V4Sample) -> dict:
    return {
        "ts": s.ts.isoformat(), "hvac": s.hvac_action or "idle", "fan": False, "indoor": s.readings[HALL],
        "outdoor": s.outdoor_f, "pred_outdoor": s.outdoor_f, "setpoint": s.heat_sp_f, "lower": s.heat_sp_f,
        "upper": 76.0, "windows_open": False, "fan_running": False, "nat_vent_active": False,
        "indoor_sensors": {HALL: s.readings[HALL]},
    }  # fmt: skip
