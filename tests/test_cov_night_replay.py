"""Edge branches of the nightly replay (Silver test-coverage).

Occupant view: the nightly check of the bedroom predictions grades each night option (closed, windows, fan) from
recorded nights. A missing forecast archive, an odd night or a damaged stored record must never stop it, and a grade
earned earlier is only replaced by new evidence.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta

from custom_components.climate_advisor import night_replay as nr
from custom_components.climate_advisor import night_strategy as ns
from custom_components.climate_advisor import predictor_v4 as p4
from tests.test_night_strategy import make_model
from tests.test_night_strategy_replay import _history as replay_history
from tests.timeline_fixtures import TZ


def _step(base: p4.V4Sample, **kw) -> p4.V4Sample:
    return dataclasses.replace(base, **kw)


def test_find_nights_without_samples_is_empty() -> None:
    assert nr.find_nights([], TZ, (22, 30), (6, 30)) == []


def test_classify_fan_then_ac_and_mixed_nights() -> None:
    base = replay_history(1)[0]
    n = 40
    fan_ac = [base] + [_step(base, fan_running=True, windows_open=False, cool_duty=1.0) for _ in range(n)]
    cls, sub, stats = nr.classify(fan_ac)
    assert cls == ns.CLS_FAN and sub == "fan:then_ac" and stats["cool_min"] >= nr.MIN_AC_MIN
    # windows open for a share between "closed" and "windows": neither option, so it is not graded as either
    k = int(n * (nr.CLOSED_MAX_SHARE + nr.WINDOWS_MIN_SHARE) / 2)
    mixed = [base] + [
        _step(base, fan_running=False, windows_open=i < k, cool_duty=0.0, heat_duty=0.0) for i in range(n)
    ]
    assert nr.classify(mixed)[:2] == ("mixed", "mixed")


def test_mass_at_an_unknown_bedtime_is_none() -> None:
    assert nr.mass_at(make_model(), [], {}, datetime(2026, 6, 1, 22, 30, tzinfo=TZ)) is None


def test_replay_night_without_a_closed_loop_result_keeps_only_the_gate_figures(monkeypatch) -> None:
    samples = replay_history(2)
    start = datetime(2026, 6, 1, 22, 30, tzinfo=TZ)
    rows = [s for s in samples if start <= s.ts <= start + timedelta(hours=8)]
    rows = [_step(r, heat_sp_f=66.0, cool_sp_f=76.0) for r in rows]
    monkeypatch.setattr(ns, "replay_recorded", lambda *a, **k: None)
    res = nr.replay_night(make_model(), TZ, start, rows[-1].ts, rows, None)
    assert "mae_f" in res and "closed_loop_mae_f" not in res


def test_run_limits_nights_to_the_since_until_dates() -> None:
    samples = replay_history(30)
    since, until = date(2026, 6, 20), date(2026, 6, 24)
    rep = nr.run(samples, TZ, bed=(22, 30), wake=(6, 30), train_days=30, since=since, until=until)
    assert rep["n_nights_found"] == 5
    assert all("2026-06-20" <= r["night"] <= "2026-06-24" for r in rep["nights"])


def test_run_reports_classes_from_ok_confidence_fits(monkeypatch) -> None:
    real_fit = p4.fit

    def ok_fit(*a, **k):
        m = real_fit(*a, **k)
        return dataclasses.replace(m, confidence="ok") if m.confidence != "none" else m

    monkeypatch.setattr(p4, "fit", ok_fit)
    rep = nr.run(replay_history(30), TZ, bed=(22, 30), wake=(6, 30), train_days=30, max_nights=7)
    assert rep["n_ok_confidence_fits"] == rep["n_nights_replayed"] > 0
    assert rep["classes_ok_fits_only"]
    for cls, block in rep["classes_ok_fits_only"].items():
        assert block["n_nights"] == rep["classes"][cls]["n_nights"]


def test_carry_class_keeps_the_old_grade_against_a_few_new_nights_that_do_not_contradict_it() -> None:
    old = ns.ClassValidation(n_nights=ns.VALIDATION_HIGH_NIGHTS, mae_f=0.5, flat_mae_f=1.5, bias_f=0.0)
    new = ns.ClassValidation(n_nights=ns.VALIDATION_MIN_NIGHTS, mae_f=0.5, flat_mae_f=1.5, bias_f=0.0)
    assert ns.validation_status(ns.NightValidation(classes={"x": new}), "x")[0] != "low"
    assert nr._carry_class(new, old) is True


def test_carry_evidence_without_new_evidence_keeps_the_old() -> None:
    old = ns.Evidence(1.0, 2.0, 3.0, 4.0, {"_ca": 5.0})
    assert nr._carry_evidence(None, old) is old


def test_evidence_from_a_malformed_record_is_none() -> None:
    assert nr.evidence_from_record({"evidence": {"fan_hours": "lots"}}) is None
    assert nr.evidence_from_record({"evidence": {"sensor_hours": {"_ca": None}}}) is None


def test_run_nightly_survives_a_failing_forecast_archive(monkeypatch) -> None:
    calls = []

    def bad_snapshots(start, end):
        calls.append((start, end))
        raise OSError("archive unreadable")

    rec = nr.run_nightly(
        chart_entries=[],
        snapshots_fn=bad_snapshots,
        tz=TZ,
        now=datetime(2026, 6, 30, 3, 0, tzinfo=TZ),
        primary="_ca",
        sensors=["_ca"],
    )
    assert calls and rec["n_nights_found"] == 0 and rec["provisional"] is True


def test_run_nightly_without_an_archive() -> None:
    rec = nr.run_nightly(
        chart_entries=[],
        snapshots_fn=None,
        tz=TZ,
        now=datetime(2026, 6, 30, 3, 0, tzinfo=TZ),
        primary="_ca",
        sensors=["_ca"],
    )
    assert rec["n_nights_replayed"] == 0
