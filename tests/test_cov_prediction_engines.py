"""Edge branches of the prediction-engine registry (Silver test-coverage).

Occupant view: a broken or half-written engine state must never crash the Status chart or the decisions; the
chart reads "learning" and the house runs on the default plan instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor import prediction_engines as pe
from custom_components.climate_advisor import predictor_v4, v4_shadow

NOW = datetime(2026, 11, 6, 20, 0, tzinfo=UTC)
V4_CFG = {"primary_engine": "v4"}


class _Boom:
    """An engine whose every read raises."""

    id = "v4"
    label = "v4"

    def confidence(self, state):
        raise RuntimeError("boom")

    def state_of(self, source):
        raise RuntimeError("boom")


def _ready_v4_source(**extra):
    return SimpleNamespace(_v4_store=SimpleNamespace(v4_model={"confidence": "medium"}), **extra)


# ------------------------------------------------------------------------------------------------ usable / _parse
def test_usable_is_false_when_the_engine_raises() -> None:
    assert pe.usable(_Boom(), SimpleNamespace()) is False


def test_parse_skips_rows_without_ts_or_temp_and_bad_timestamps() -> None:
    rows = [
        {"ts": 123, "temp": 70},  # not a string
        {"ts": NOW.isoformat(), "temp": None},  # no temperature
        {"ts": "not-a-date", "temp": 70},  # unparseable
        {"ts": NOW.isoformat(), "temp": "71.5"},
    ]
    assert pe._parse(rows) == [(NOW, 71.5)]
    assert pe._parse(None) == []


# ------------------------------------------------------------------------------------------------ V3 engine
def test_v3_fit_uses_the_learning_engine_or_the_stored_model() -> None:
    v3 = pe.ENGINES["v3"]
    learning = SimpleNamespace(get_thermal_model=lambda: {"confidence": "high"})
    assert v3.fit({"learning": learning}) == {"confidence": "high"}
    assert v3.fit({"thermal_model": {"confidence": "low"}}) == {"confidence": "low"}
    assert v3.fit({}) == {}


def test_v3_predict_without_a_builder_or_rows_is_empty_and_trims_to_the_horizon() -> None:
    v3 = pe.ENGINES["v3"]
    assert v3.predict({}, 24, {}) == {}
    assert v3.predict({}, 24, {"build_predicted_indoor": lambda **kw: []}) == {}
    rows = [{"ts": (NOW + timedelta(hours=h)).isoformat(), "temp": 70 + h} for h in range(5)]
    out = v3.predict({}, 2, {"build_predicted_indoor": lambda **kw: rows})
    assert [p[1] for p in out["_ca"]] == [70.0, 71.0, 72.0]  # 0..2 h only


def test_v3_confidence_control_model_and_control_path() -> None:
    v3 = pe.ENGINES["v3"]
    assert v3.confidence("not a mapping") == "none"
    assert v3.control_model({"a": 1}, None) == {"a": 1}  # no base: its own state
    assert v3.control_model({"a": 1}, {"b": 2}) == {"b": 2}
    src = SimpleNamespace(_last_predicted_indoor=[{"ts": NOW.isoformat(), "temp": 69.0}])
    assert v3.control_path(src, NOW) == {"_ca": [(NOW, 69.0)]}
    assert v3.future_path(SimpleNamespace()) == {}
    assert v3.state_of(SimpleNamespace(_thermal_model="bad")) == {}


def test_v3_past_merges_its_own_archive_for_other_leads_and_ignores_other_sensors() -> None:
    v3 = pe.ENGINES["v3"]
    arch = SimpleNamespace(series=lambda sensor, lead, start, end: {1800: 60.0, 3600: 61.0})
    src = SimpleNamespace(_pred_archive={1800: 70.0}, _v3_archive=arch)
    assert v3.past(src, "_ca", "4h", 0, 7200) == {1800: 70.0, 3600: 61.0}  # the chart-log archive wins
    assert v3.past(src, "_ca", "1h", 0, 7200) == {1800: 60.0, 3600: 61.0}
    assert v3.past(src, "climate.hall", "4h", 0, 7200) == {}


# ------------------------------------------------------------------------------------------------ V4 engine
def test_v4_fit_returns_the_model_from_run_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def fake_run_fit(**kw):
        seen.update(kw)
        return {"confidence": "low"}, 1.5, "summary"

    monkeypatch.setattr(v4_shadow, "run_fit", fake_run_fit)
    assert pe.ENGINES["v4"].fit({"rows": [1, 2]}) == {"confidence": "low"}
    assert seen == {"rows": [1, 2]}


def test_v4_predict_trims_each_sensor_and_drops_empty_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    v4 = pe.ENGINES["v4"]
    pts = [(NOW + timedelta(hours=h), 70 + h) for h in range(4)]
    monkeypatch.setattr(v4_shadow, "run_cycle", lambda **kw: ({"climate.hall": pts, "sensor.empty": []}, 0.0))
    out = v4.predict({}, 1, {})
    assert out == {"climate.hall": [(NOW, 70.0), (NOW + timedelta(hours=1), 71.0)]}
    monkeypatch.setattr(v4_shadow, "run_cycle", lambda **kw: ({}, 0.0))
    assert v4.predict({}, 1, {}) == {}


def test_v4_paths_without_predictions_or_primary_have_no_ca_alias() -> None:
    v4 = pe.ENGINES["v4"]
    src = SimpleNamespace(_v4_store=SimpleNamespace(v4_model={"primary": "climate.other"}))
    assert v4._paths(src, {"predictions": "bad"}) == {}
    assert v4._paths(src, None) == {}
    out = v4._paths(src, {"predictions": {"climate.hall": [(NOW, 70.0)]}})
    assert out == {"climate.hall": [(NOW, 70.0)]}  # the primary is not in the predictions: no _ca line


def test_v4_control_model_without_a_mapping_state_is_the_base() -> None:
    assert pe.ENGINES["v4"].control_model(None, {"x": 1}) == {"x": 1}


def test_v4_control_path_rejects_bad_naive_future_and_stale_stamps() -> None:
    v4 = pe.ENGINES["v4"]
    preds = {"predictions": {"_ca": [(NOW, 70.0)]}}
    assert v4.control_path(SimpleNamespace(_v4_control_future={**preds, "at": "garbage"}), NOW) == {}
    naive = NOW.replace(tzinfo=None).isoformat()
    assert v4.control_path(SimpleNamespace(_v4_control_future={**preds, "at": naive}), NOW) == {}
    old = (NOW - timedelta(hours=2)).isoformat()
    assert v4.control_path(SimpleNamespace(_v4_control_future={**preds, "at": old}), NOW) == {}
    fresh = SimpleNamespace(_v4_control_future={**preds, "at": NOW.isoformat()}, _v4_store=None)
    assert v4.control_path(fresh, NOW) == {"_ca": [(NOW, 70.0)]}


def test_v4_past_without_an_archive_is_empty() -> None:
    assert pe.ENGINES["v4"].past(SimpleNamespace(), "_ca", "4h", 0, 1) == {}


# ------------------------------------------------------------------------------------------------ engine_state
def test_engine_state_survives_an_engine_that_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(pe.ENGINES, "v4", _Boom())
    monkeypatch.setattr(pe, "usable", lambda e, s: (_ for _ in ()).throw(RuntimeError("x")))
    s = pe.engine_state(SimpleNamespace(), V4_CFG)
    v4 = next(c for c in s["engines"] if c["id"] == "v4")
    assert v4["usable"] is False and v4["confidence"] == "none"
    assert s["fallback"] is True


def test_engine_state_refit_notice_failure_reads_as_no_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(rec):
        raise RuntimeError("x")

    monkeypatch.setattr(v4_shadow, "unusable_fit_notice", broken)
    src = _ready_v4_source(_v4_fit_running=False)
    src._v4_store.unusable_fit = {"count": 3}
    s = pe.engine_state(src, V4_CFG)
    assert s["notice"] == "" and s["refit_note"] == "" and s["primary_state"] == "ready"


# ------------------------------------------------------------------------------------------------ control feed
def test_control_stepper_none_for_v3_primary_and_unloadable_model(monkeypatch: pytest.MonkeyPatch) -> None:
    src = _ready_v4_source(_thermal_model={"confidence": "high"})
    assert pe.control_stepper(src, {"primary_engine": "v3"}) is None
    monkeypatch.setattr(predictor_v4, "from_dict", lambda d: None)
    assert pe.control_stepper(src, V4_CFG) is None


def test_control_stepper_failure_falls_back_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(predictor_v4, "from_dict", lambda d: (_ for _ in ()).throw(ValueError("bad")))
    assert pe.control_stepper(_ready_v4_source(), V4_CFG) is None


def test_control_model_a_broken_engine_reads_as_not_learned(monkeypatch: pytest.MonkeyPatch) -> None:
    v4 = pe.ENGINES["v4"]
    monkeypatch.setattr(v4, "control_model", lambda state, base: (_ for _ in ()).throw(RuntimeError("x")))
    out = pe.control_model(_ready_v4_source(), V4_CFG, {"keep": 1})
    assert out["keep"] == 1
    for k, v in pe.CONTROL_BLANK_MODEL.items():
        assert out[k] == v


def test_control_path_a_broken_engine_is_none_and_empty_path_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    v4 = pe.ENGINES["v4"]
    src = _ready_v4_source()
    assert pe.control_path(src, V4_CFG, NOW) is None  # usable, but nothing computed this cycle
    monkeypatch.setattr(v4, "control_path", lambda source, now: (_ for _ in ()).throw(RuntimeError("x")))
    assert pe.control_path(src, V4_CFG, NOW) is None


# ------------------------------------------------------------------------------------------------ resample_path
def test_resample_single_point_path_holds_its_value_and_naive_grid_takes_the_path_zone() -> None:
    grid = [NOW.replace(tzinfo=None), NOW + timedelta(minutes=30)]
    out = pe.resample_path([(NOW, 70.04)], grid)
    assert out == [{"ts": grid[0].isoformat(), "temp": 70.0}]  # the 30-min point lies past the path: left out
    assert pe.resample_path([], grid) == []


def test_control_stepper_steps_with_the_primary_house_model(monkeypatch: pytest.MonkeyPatch) -> None:
    house = {k: float(i) for i, k in enumerate(predictor_v4.HOUSE_KEYS)}  # q_h = 7, q_c = 8
    monkeypatch.setattr(predictor_v4, "from_dict", lambda d: SimpleNamespace(house=house))
    calls = []

    def fake_free_step(h, t, mass, **kw):
        calls.append((h, t, mass, kw))
        return t + 1.0, mass + 0.5

    monkeypatch.setattr(predictor_v4, "free_step", fake_free_step)
    stepper = pe.control_stepper(_ready_v4_source(), V4_CFG)
    assert stepper is not None and stepper.heat_gain == 7.0 and stepper.cool_gain == 8.0
    assert stepper.step(70.0, 69.0, 50.0, 0.0, 1.0, 14.0) == (71.0, 69.5)
    assert calls[0][3] == {"out": 50.0, "win": 0.0, "fan": 1.0, "hour": 14.0}
