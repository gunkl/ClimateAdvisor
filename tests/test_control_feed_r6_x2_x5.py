"""X2/X5 of the #1230 R6 review: the decisions never borrow v3's curve, and v3's curve exists only when asked for.

Occupant view: with v4 as the primary engine, a hiccup inside the pre-decision prediction must not quietly hand the
ceiling guard v3's modelled curve (another engine acting behind the occupant's back), and at most two engines
compute. Also the exact-comparison ``hvac_mode_writes`` assertion of the simulation harness.
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.climate_advisor import prediction_engines as pe
from tests.test_control_feed_r6b import _ready, _refresh
from tests.test_v4_shadow_wiring import _build
from tools.sim_harness import outcomes
from tools.sim_harness.scenario_model import v4_model_from_spec

V3_CURVE = [{"ts": "2026-01-01T12:00:00+00:00", "temp": 99.0}]


def _v4_coord(*, shadow: str = "v3"):
    coord, _hass, scheduler = _build()
    model = v4_model_from_spec({"confidence": "ok", "primary": coord.config["climate_entity"]})
    _ready(coord, scheduler, "v4", model, shadow=shadow)
    coord._last_predicted_indoor = list(V3_CURVE)  # v3's curve, "hot": must never reach a decision
    return coord, scheduler


def test_a_failure_in_the_pre_decision_step_leaves_the_schedule_estimate_not_v3s_curve(monkeypatch) -> None:
    coord, scheduler = _v4_coord()

    def boom(*_a, **_k):
        raise RuntimeError("resample failed")

    monkeypatch.setattr(pe, "resample_path", boom)
    _refresh(coord, scheduler)
    curve = coord.control_predicted_indoor()
    assert curve is not None and curve != V3_CURVE
    assert curve == coord._control_path


def test_if_even_the_schedule_estimate_fails_the_decisions_see_no_curve(monkeypatch) -> None:
    coord, scheduler = _v4_coord()

    async def boom():
        raise RuntimeError("no estimate")

    monkeypatch.setattr(pe, "resample_path", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(coord, "_build_no_model_path", boom)
    _refresh(coord, scheduler)
    assert coord.control_predicted_indoor() == []


def test_before_the_first_refresh_a_non_v3_primary_has_no_curve_not_v3s() -> None:
    coord, scheduler = _v4_coord()
    coord._control_path = None  # nothing refreshed yet
    assert coord.control_predicted_indoor() is None


def test_v3_primary_still_reads_its_own_curve() -> None:
    coord, scheduler = _v4_coord(shadow="none")
    coord.config["primary_engine"] = "v3"
    coord._control_path = None
    assert coord.control_predicted_indoor() == V3_CURVE


def test_at_most_two_engines_run_and_v3_observations_are_not_an_engine() -> None:
    assert pe.running_ids({"primary_engine": "v4", "shadow_engine": "none"}) == ["v4"]
    assert pe.running_ids({"primary_engine": "v4", "shadow_engine": "v3"}) == ["v4", "v3"]
    assert pe.running_ids({"primary_engine": "v3", "shadow_engine": "v4"}) == ["v3", "v4"]
    assert pe.observations_running() is True


def test_v3_curve_is_not_built_unless_v3_is_primary_or_shadow() -> None:
    coord, _scheduler = _v4_coord(shadow="none")
    assert coord.engine_running("v3") is False and coord.engine_running("v4") is True
    coord.config["shadow_engine"] = "v3"
    assert coord.engine_running("v3") is True
    coord.config.update({"primary_engine": "v3", "shadow_engine": "none"})
    assert coord.engine_running("v3") is True and coord.engine_running("v4") is False


# ------------------------------------------------------------------------------ hvac_mode_writes assertion
def _call(ts: str, service: str, **data) -> dict:
    return {"domain": "climate", "service": service, "ts": ts, "data": {"entity_id": "climate.x", **data}}


LOG = [
    _call("2026-05-20T06:01:00", "set_hvac_mode", hvac_mode="cool"),
    _call("2026-05-20T06:01:00", "set_temperature", hvac_mode="cool", temperature=72.0),
    _call("2026-05-20T12:30:00", "set_hvac_mode", hvac_mode="heat"),
    _call("2026-05-20T14:30:00", "set_hvac_mode", hvac_mode="cool"),
]


def _check(since: str, at: str, values: list[str]):
    res = SimpleNamespace(action_log=LOG, event_log=[], engine_state={})
    return outcomes.check_assertion(res, {"at": at, "expect": "hvac_mode_writes", "since": since, "values": values})


def test_hvac_mode_writes_is_an_exact_ordered_comparison_of_real_mode_calls() -> None:
    assert _check("2026-05-20T00:00:00", "2026-05-20T23:59:00", ["cool", "heat", "cool"]) == "hvac_mode_writes"
    assert _check("2026-05-20T00:00:00", "2026-05-20T23:59:00", ["cool", "heat"]) is False  # an extra real write
    assert (
        _check("2026-05-20T00:00:00", "2026-05-20T23:59:00", ["cool", "cool", "heat", "cool"]) is False
    )  # a missing one
    assert _check("2026-05-20T00:00:00", "2026-05-20T23:59:00", ["heat", "cool", "cool"]) is False  # wrong order
    assert _check("2026-05-20T06:01:00", "2026-05-20T12:29:00", []) == "hvac_mode_writes"  # since is exclusive
    assert _check("2026-05-20T06:00:59", "2026-05-20T12:29:00", []) is False
    assert _check("2026-05-20T12:30:00", "2026-05-20T14:30:00", ["cool"]) == "hvac_mode_writes"  # at is inclusive


# ----------------------------------------------- chart-log pred_indoor is null when v3 is neither primary nor shadow
def test_null_pred_indoor_breaks_no_consumer(tmp_path) -> None:
    """With v4 primary and no v3 comparison, v3's 4 h archive is not written, so chart-log ``pred_indoor`` is null.

    Occupant view: nothing they see changes except that the (hidden) v3 line has no data. The Prediction accuracy
    page, the control-quality report, the chart-log hourly aggregation and the chart's past-prediction series all
    treat null as "no value"."""
    from custom_components.climate_advisor import prediction_accuracy as pa
    from custom_components.climate_advisor.control_quality import build_control_quality
    from tests.test_v4_shadow_endpoint import _NOW, _archive, _rows

    rows = [{**r, "pred_indoor": None} for r in _rows(days=3)]
    out = pa.build_prediction_accuracy(
        zone="zoneA",
        sensor="_ca",
        lead="4h",
        range_str="24h",
        before=None,
        now=_NOW,
        unit="fahrenheit",
        chart_rows=rows,
        archive=_archive(tmp_path),
        backtest=None,
        model={"has_model": True, "confidence": "ok", "fitted_at": "2026-11-06T03:15:00+00:00", "span_days": 3.0},
        sensors_meta=[{"id": "_ca", "name": "Climate Advisor reading", "role": "the reading Climate Advisor uses"}],
    )
    assert out["points"]  # v4's live archive still draws
    assert all(p.get("v3") is None for p in out["points"])

    cq = build_control_quality(now=_NOW, mode_changes=[], chart_entries=rows)
    for window in cq["windows"].values():
        lead = window["prediction_error"]["archive_lead"]
        assert lead["n"] == 0 and lead["mean_abs_f"] is None
