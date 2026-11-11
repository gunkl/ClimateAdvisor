"""The primary prediction engine drives the prediction-dependent decisions (Issue #1230 R6b).

Occupant view: pre-heat/TOU banking, the ceiling guard's "AC on ahead of the heat" and the nat-vent plan's close time
read the prediction engine chosen in Settings (v4 by default). While that engine has not finished learning the house
runs on the "learning not finished" defaults, never on the other engine; the optional comparison engine never acts.
Real coordinator (headless) + the real v4 predictor.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor import prediction_engines as pe
from custom_components.climate_advisor import predictor_v4 as p4
from tests.test_v4_shadow_wiring import _START, _bands, _build, _hourly
from tools.sim_harness._loop import run_coro
from tools.sim_harness.scenario_model import v4_model_from_spec

V3_MODEL = {
    "confidence": "high",
    "confidence_k_passive": "high",
    "k_passive": -0.15,
    "k_active_heat": 2.0,
    "k_active_cool": -2.0,
    "heating_rate_f_per_hour": 2.0,
    "cooling_rate_f_per_hour": 2.0,
    "swing_heat_f": 1.25,
    "k_vent_window": -0.4,
}


def _src(model: dict | None) -> SimpleNamespace:
    return SimpleNamespace(_v4_store=SimpleNamespace(v4_model=model), _v4_fit_running=False)


# --------------------------------------------------------------------------------------------- registry, pure
@pytest.mark.parametrize("grade", ["low", "ok"])
def test_a_fitted_v4_model_is_usable_whatever_grade_v4_gives_it(grade: str) -> None:
    """v4 grades its fit none/low/ok. 'ok' was missing from the usable set, so a fully fitted v4 read as learning."""
    assert pe.usable(pe.ENGINES["v4"], _src({"confidence": grade, "primary": "climate.x"}))
    assert not pe.usable(pe.ENGINES["v4"], _src({"confidence": "none", "primary": "climate.x"}))
    assert not pe.usable(pe.ENGINES["v4"], _src(None))


def test_control_rates_are_the_net_gain_of_one_hour_of_heat_through_the_hidden_mass() -> None:
    spec = {"confidence": "ok", "house": {"a_o": 0.06, "q_h": 10.0, "q_c": 11.0, "c_hm": 0.6, "c_mh": 0.2}}
    rates = p4.control_rates(p4.from_dict(v4_model_from_spec(spec)))
    # independent Euler: 4 steps of 15 min, mass starts level with the air, no leak, no sun
    h = m = 68.0
    for _ in range(4):
        dh = 0.6 * (m - h) + 10.0
        dm = 0.2 * (h - m)
        h, m = h + dh * 0.25, m + dm * 0.25
    assert rates["k_active_heat"] == pytest.approx(h - 68.0, abs=1e-9)
    # the first hour is slower than the heater's gain, not absurdly so
    assert 0.6 * 10.0 < rates["k_active_heat"] < 10.0
    assert rates["k_active_cool"] < 0 and abs(rates["k_active_cool"]) < 11.0
    assert rates["heating_rate_f_per_hour"] == round(rates["k_active_heat"], 2)
    assert rates["k_passive"] == pytest.approx(-0.06)
    # The gate bridge was removed in #1512 (k_vent_window is a windows-open rate, not a closed-house rate).
    assert "k_passive_via_bridge" not in rates
    assert (rates["confidence"], rates["confidence_k_passive"]) == ("medium", "medium")
    assert p4.control_rates(p4.from_dict(v4_model_from_spec({"confidence": "low"})))["confidence"] == "low"
    assert p4.control_rates(p4.from_dict(v4_model_from_spec({"confidence": "none"}))) == {}


def test_control_model_v3_is_the_very_object_the_learning_engine_holds() -> None:
    assert pe.control_model(_src(None), {"primary_engine": "v3"}, V3_MODEL) is V3_MODEL
    assert pe.control_model(_src(None), {"primary_engine": "v3"}, None) is None


def test_control_model_v4_lays_its_scalars_over_v3_and_keeps_v3_observations() -> None:
    model = v4_model_from_spec({"confidence": "ok", "house": {"a_o": 0.06, "q_h": 10.0, "q_c": 10.0}})
    out = pe.control_model(_src(model), {"primary_engine": "v4"}, V3_MODEL)
    assert out["k_passive"] == pytest.approx(-0.06) and out["k_active_heat"] != V3_MODEL["k_active_heat"]
    assert out["swing_heat_f"] == 1.25  # observations stay v3's
    # Issue #1505: the window and fan rates are v4's own now (a_o + a_w / a_f), not v3's learner's. The old assertion
    # (``k_vent_window == -0.4``, v3's) pinned a v3 windows rate next to v4's envelope rate: the timeline's
    # windows-vs-envelope check then compared two different models, and on Zone 1 passed on an inflated v3 rate.
    house = model["house"]
    assert out["k_vent_window"] == pytest.approx(-(0.06 + house["a_w"]))
    assert out["k_vent_fan"] == pytest.approx(-(0.06 + house["a_f"]))
    assert V3_MODEL["k_passive"] == -0.15  # the input is not mutated


@pytest.mark.parametrize("model", [None, {"confidence": "none", "primary": "climate.x"}])
def test_control_model_without_a_usable_fit_is_the_learning_not_finished_model(model: dict | None) -> None:
    out = pe.control_model(_src(model), {"primary_engine": "v4"}, V3_MODEL)
    for key, blank in pe.CONTROL_BLANK_MODEL.items():
        assert out[key] == blank
    assert out["swing_heat_f"] == 1.25  # only the prediction-driving scalars are blanked
    assert pe.control_path(_src(model), {"primary_engine": "v4"}, _START) is None


def test_the_shadow_engine_never_changes_what_the_decisions_read() -> None:
    model = v4_model_from_spec({"confidence": "ok"})
    src = _src(model)
    alone = pe.control_model(src, {"primary_engine": "v4", "shadow_engine": "none"}, V3_MODEL)
    with_shadow = pe.control_model(src, {"primary_engine": "v4", "shadow_engine": "v3"}, V3_MODEL)
    assert alone == with_shadow
    src2 = _src(None)  # primary has no fit, shadow v3 is perfectly healthy: still the defaults
    out = pe.control_model(src2, {"primary_engine": "v4", "shadow_engine": "v3"}, V3_MODEL)
    assert out["confidence"] == "none" and out["k_passive"] is None


def test_an_engine_that_cannot_drive_decisions_stays_on_the_defaults() -> None:
    class Display:
        id, label, state_file, archive_file = "vx", "vx", "f", "g"

        def fit(self, history): ...
        def predict(self, state, horizon_hours, plan_inputs):
            return {}

        def confidence(self, state):
            return "high"

        def state_of(self, source):
            return {"confidence": "high"}

        def future_path(self, source):
            return {}

        def past(self, *a):
            return {}

    pe.register(Display())
    try:
        assert not pe.control_ready(SimpleNamespace(), {"primary_engine": "vx"})
        out = pe.control_model(SimpleNamespace(), {"primary_engine": "vx"}, V3_MODEL)
        assert out["confidence"] == "none"
        assert pe.engine_state(SimpleNamespace(), {"primary_engine": "vx"})["control"]["source"] == "defaults"
    finally:
        pe.unregister("vx")


def test_resample_path_puts_the_engines_15_minute_points_on_the_hourly_plus_midpoint_grid() -> None:
    t0 = _START
    pts = [(t0 + timedelta(minutes=15 * k), 70.0 + k) for k in range(1, 9)]  # 15..120 min
    grid = [t0 + timedelta(minutes=m) for m in (0, 30, 60, 90, 120, 150)]
    out = pe.resample_path(pts, grid, anchor=(t0, 70.0))
    assert [round(e["temp"], 1) for e in out] == [70.0, 72.0, 74.0, 76.0, 78.0]  # 150 min is past the path: left out
    assert [e["ts"] for e in out] == [g.isoformat() for g in grid[:5]]
    assert pe.resample_path([], grid) == []


# ----------------------------------------------------------------------------------- coordinator, real predictor
def _ready(coord, scheduler, primary: str = "v4", model: dict | None = None, *, shadow: str = "none") -> None:
    now = scheduler.now()
    coord.config["primary_engine"] = primary
    coord.config["shadow_engine"] = shadow
    coord._hourly_forecast_temps = _hourly(now, base=55.0)
    coord._target_band_schedule = _bands(now)
    coord._v4_store.v4_model = model


def _refresh(coord, scheduler) -> None:
    with scheduler.installed():
        run_coro(coord._refresh_control_prediction())


def test_v3_primary_hands_the_decisions_v3s_own_curve_and_model_unchanged() -> None:
    coord, _hass, scheduler = _build()
    _ready(coord, scheduler, "v3")
    coord._last_predicted_indoor = [{"ts": scheduler.now().isoformat(), "temp": 71.0}]
    _refresh(coord, scheduler)
    assert coord._control_path is None
    assert coord.control_predicted_indoor() is coord._last_predicted_indoor
    base = {"confidence": "low"}
    assert coord.control_thermal_model(base) is base


def test_v4_primary_with_a_fit_feeds_the_decisions_its_own_no_ac_path_on_the_v3_grid(caplog) -> None:
    coord, _hass, scheduler = _build()
    model = v4_model_from_spec(
        {"confidence": "ok", "primary": coord.config["climate_entity"], "house": {"a_o": 0.08, "s": 0.0}}
    )
    _ready(coord, scheduler, "v4", model)
    coord._last_predicted_indoor = [{"ts": scheduler.now().isoformat(), "temp": 12.3}]  # v3's curve must not leak
    with caplog.at_level(logging.INFO):
        _refresh(coord, scheduler)
    path = coord.control_predicted_indoor()
    assert path and path is coord._control_path and all(set(e) == {"ts", "temp"} for e in path)
    assert all(50.0 < e["temp"] < 90.0 for e in path), "the path is in internal degrees F, not v3's 12.3"
    with scheduler.installed():
        grid = coord._control_grid()
    assert {e["ts"] for e in path} <= {g.isoformat() for g in grid}, "every point sits on v3's hourly + midpoint grid"
    assert coord.control_thermal_model({"confidence": "none"})["confidence"] == "medium"
    assert "Decisions now use prediction engine v4 (was: startup)" in caplog.text
    # the house at 70 F drifting to a 55-64 F outdoor temperature with no AC and no heat: the path falls
    assert path[-1]["temp"] < path[0]["temp"]


def test_primary_without_a_fit_runs_the_schedule_estimate_and_the_blank_model_logged_once_per_episode(caplog) -> None:
    coord, _hass, scheduler = _build()
    _ready(coord, scheduler, "v4", None)
    coord._last_predicted_indoor = [{"ts": scheduler.now().isoformat(), "temp": 99.0}]
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            _refresh(coord, scheduler)
    assert coord._control_path is not None and all(e["temp"] != 99.0 for e in coord.control_predicted_indoor())
    assert coord.control_thermal_model(dict(V3_MODEL))["k_passive"] is None
    assert coord.automation_engine._control_model()["confidence"] == "none"  # the engine reads the same
    lines = [r for r in caplog.records if "has no usable fit yet" in r.getMessage()]
    assert len(lines) == 1, "once per episode, not once per cycle"
    assert pe.engine_state(coord, coord.config)["control"] == {
        "engine": "v4",
        "source": "defaults",
        "ready": False,
        "text": pe.engine_state(coord, coord.config)["control"]["text"],
    }
    assert "default plan" in pe.engine_state(coord, coord.config)["control"]["text"]


def test_switching_v3_to_v4_without_a_model_defaults_then_v4_when_the_fit_lands_then_back_to_v3(caplog) -> None:
    coord, _hass, scheduler = _build()
    v3_curve = [{"ts": scheduler.now().isoformat(), "temp": 71.0}]
    _ready(coord, scheduler, "v3")
    coord._last_predicted_indoor = v3_curve
    with caplog.at_level(logging.INFO):
        _refresh(coord, scheduler)
        assert coord.control_predicted_indoor() is v3_curve
        # the occupant switches the primary to v4 (after the reload): no v4 model yet
        _ready(coord, scheduler, "v4", None)
        coord._last_predicted_indoor = v3_curve
        _refresh(coord, scheduler)
        assert coord.control_predicted_indoor() is not v3_curve
        assert coord.control_thermal_model(dict(V3_MODEL))["k_active_heat"] is None
        # the first fit lands
        coord._v4_store.v4_model = v4_model_from_spec(
            {"confidence": "low", "primary": coord.config["climate_entity"], "house": {"a_o": 0.05}}
        )
        _refresh(coord, scheduler)
        assert coord.control_thermal_model(dict(V3_MODEL))["confidence"] == "low"
        # and back to v3
        coord.config["primary_engine"] = "v3"
        _refresh(coord, scheduler)
    assert coord.control_predicted_indoor() is v3_curve
    msgs = [r.getMessage() for r in caplog.records]
    assert any("has no usable fit yet" in m for m in msgs)
    assert any("Decisions now use prediction engine v4 (was: defaults)" in m for m in msgs)
    assert any("Decisions now use prediction engine v3 (was: v4)" in m for m in msgs)


def test_the_shadow_engine_is_never_consulted_for_the_decisions() -> None:
    coord, _hass, scheduler = _build()
    model = v4_model_from_spec({"confidence": "ok", "primary": coord.config["climate_entity"]})
    _ready(coord, scheduler, "v4", model)
    _refresh(coord, scheduler)
    alone = [dict(e) for e in coord.control_predicted_indoor()]
    _ready(coord, scheduler, "v4", model, shadow="v3")
    coord._last_predicted_indoor = [{"ts": scheduler.now().isoformat(), "temp": 12.3}]
    _refresh(coord, scheduler)
    assert [dict(e) for e in coord.control_predicted_indoor()] == alone


def test_a_stale_pre_decision_prediction_is_not_trusted() -> None:
    coord, _hass, scheduler = _build()
    model = v4_model_from_spec({"confidence": "ok", "primary": coord.config["climate_entity"]})
    _ready(coord, scheduler, "v4", model)
    _refresh(coord, scheduler)
    assert pe.control_path(coord, coord.config, scheduler.now())
    assert pe.control_path(coord, coord.config, scheduler.now() + timedelta(minutes=50)) is None


def test_a_failed_pre_decision_prediction_falls_back_to_the_schedule_estimate_for_the_cycle(monkeypatch) -> None:
    from custom_components.climate_advisor import v4_shadow

    coord, _hass, scheduler = _build()
    model = v4_model_from_spec({"confidence": "ok", "primary": coord.config["climate_entity"]})
    _ready(coord, scheduler, "v4", model)

    def boom(**_kw):
        raise RuntimeError("predict failed")

    monkeypatch.setattr(v4_shadow, "run_cycle", boom)
    _refresh(coord, scheduler)
    assert coord._v4_control_future is None
    assert coord._control_path is not None  # the schedule estimate, never an exception into the cycle


def test_the_pre_decision_prediction_writes_no_shadow_state_and_costs_milliseconds() -> None:
    coord, _hass, scheduler = _build()
    model = v4_model_from_spec({"confidence": "ok", "primary": coord.config["climate_entity"]})
    _ready(coord, scheduler, "v4", model)
    store = coord._v4_store
    before = (store.v4_mass_f, store.v4_mass_at, store.dirty, coord._v4_last_future, coord._v4_archive.dirty)
    t0 = time.perf_counter()
    for _ in range(20):
        _refresh(coord, scheduler)
    per_cycle_ms = (time.perf_counter() - t0) * 1000.0 / 20
    assert (store.v4_mass_f, store.v4_mass_at, store.dirty, coord._v4_last_future, coord._v4_archive.dirty) == before
    assert per_cycle_ms < 150.0, f"control refresh cost {per_cycle_ms:.1f} ms per cycle"
    print(f"control refresh: {per_cycle_ms:.1f} ms per cycle (headless, real predictor)")


def test_control_rates_export_v4s_window_and_fan_rates_in_v3s_keys_1505() -> None:
    """Issue #1505: v4 fits the window and fan leak with everything else; the timeline reads them like k_passive."""
    spec = {"confidence": "ok", "house": {"a_o": 0.05, "a_w": 0.013, "a_f": 0.18}}
    rates = p4.control_rates(p4.from_dict(v4_model_from_spec(spec)))
    assert rates["k_vent_window"] == pytest.approx(-(0.05 + 0.013))
    assert rates["k_vent_fan"] == pytest.approx(-(0.05 + 0.18))
    assert rates["confidence_k_vent"] == "medium"


def test_a_frozen_window_or_fan_term_is_not_exported_1505() -> None:
    """A term v4 never saw data for keeps its start value: that is not a measurement, so it is not handed on."""
    model = v4_model_from_spec({"confidence": "ok", "house": {"a_o": 0.05, "a_w": 0.1, "a_f": 0.3}})
    model.setdefault("fit", {})["frozen"] = ["a_w", "a_f"]
    rates = p4.control_rates(p4.from_dict(model))
    assert rates["k_vent_window"] is None and rates["k_vent_fan"] is None
    assert rates["confidence_k_vent"] == "none"
    blank = pe.CONTROL_BLANK_MODEL
    assert blank["k_vent_window"] is None and blank["k_vent_fan"] is None and blank["confidence_k_vent"] == "none"
