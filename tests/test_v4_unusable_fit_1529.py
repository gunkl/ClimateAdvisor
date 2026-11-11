"""Issue #1529: an unusable nightly v4 refit no longer replaces the learned model the decisions use.

Occupant view: one night with too little data (a sensor outage, a gap in the chart log) or a calm week in which the
refit could not beat "no change" used to replace the stored model with a confidence-"none" one, so pre-heat, the
time-of-use bank, setback depth and the ceiling guard fell back to the "not learned yet" defaults until a usable fit
came back. An unusable fit is not evidence against the model (owner rule, D35). The previous usable model stays in
use; the refit is recorded for the Debug tab and the log; a usable fit replaces the model as before. A home that never
had a usable model behaves exactly as before. Runs the REAL coordinator headless with a fake v4 engine for the refit.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from custom_components.climate_advisor import prediction_engines as pe
from custom_components.climate_advisor import predictor_v4 as p4
from custom_components.climate_advisor import v4_shadow

from .test_predictor_v4 import HALL, T0, TZ, simulate
from .test_v4_shadow_wiring import _build, _fake_model_dict, _seed_cycle_inputs, _store
from .v4_fake_predictor import install


@pytest.fixture(scope="module")
def learned() -> dict:
    """A real usable v4 model (heating learned), as the store holds it."""
    samples, _t, _ = simulate(T0, 13, noise=0.03)
    m = p4.fit(samples, primary=HALL, sensors=[HALL], tz=TZ, now=samples[-1].ts + timedelta(minutes=1))
    assert m.confidence != "none"
    return p4.to_dict(m)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch):
    return install(monkeypatch)


def _v4_primary(coord) -> None:
    coord.config.update({"primary_engine": "v4", "shadow_engine": "none"})


def _refit(coord, scheduler, reason: str = "nightly") -> None:
    with scheduler.installed():
        assert coord._v4_start_fit(reason)
        scheduler.advance_by(0)


def test_an_unusable_refit_keeps_the_learned_model_for_the_decisions(fake, learned, caplog) -> None:
    coord, _hass, scheduler = _build()
    _v4_primary(coord)
    _seed_cycle_inputs(coord, scheduler)
    coord._v4_store.v4_model = dict(learned)
    before = pe.control_model(coord, coord.config, {})
    assert pe.control_ready(coord, coord.config) and before["k_active_heat"] > 0
    fake.confidence = "none"
    with caplog.at_level(logging.INFO):
        _refit(coord, scheduler)
    assert coord._v4_store.v4_model == learned
    assert pe.control_ready(coord, coord.config)
    after = pe.control_model(coord, coord.config, {})
    assert after["k_active_heat"] == before["k_active_heat"] and after["confidence"] == before["confidence"]
    rec = coord._v4_store.unusable_fit
    assert rec is not None and rec["kept_fitted_at"] == learned["fitted_at"]
    text = f"keeping the model from {learned['fitted_at'][:10]}"
    assert any("last refit unusable" in r.getMessage() and text in r.getMessage() for r in caplog.records)
    summary = coord.v4_model_summary()
    assert summary["confidence"] == learned["confidence"]
    assert (
        summary["unusable_refit"]["text"].startswith("last refit unusable (")
        and text in summary["unusable_refit"]["text"]
    )


def test_a_usable_refit_then_replaces_it(fake, learned) -> None:
    coord, _hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    coord._v4_store.v4_model = dict(learned)
    fake.confidence = "none"
    _refit(coord, scheduler)
    assert coord._v4_store.unusable_fit is not None
    coord._v4_fit_running = False
    fake.confidence = "ok"
    _refit(coord, scheduler)
    assert coord._v4_store.v4_model["confidence"] == "ok"
    assert coord._v4_store.v4_model["fitted_at"] != learned["fitted_at"]
    assert coord._v4_store.unusable_fit is None
    assert coord.v4_model_summary()["unusable_refit"] is None


@pytest.mark.parametrize("stored", [None, "none"])
def test_a_home_that_never_had_a_usable_model_behaves_as_before(fake, stored) -> None:
    coord, _hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    coord._v4_store.v4_model = None if stored is None else _fake_model_dict(coord, confidence="none")
    fake.confidence = "none"
    _refit(coord, scheduler)
    assert coord._v4_store.v4_model is not None and coord._v4_store.v4_model["confidence"] == "none"
    assert coord._v4_store.unusable_fit is None
    assert not pe.control_ready(coord, coord.config | {"primary_engine": "v4", "shadow_engine": "none"})


def test_save_and_load_across_the_unusable_night(fake, learned) -> None:
    coord, _hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    coord._v4_store.v4_model = dict(learned)
    fake.confidence = "none"
    _refit(coord, scheduler)
    fresh = _store(coord)
    fresh.load()
    assert fresh.v4_model == learned
    assert fresh.unusable_fit == coord._v4_store.unusable_fit and fresh.unusable_fit["kept_fitted_at"]


def test_the_pure_rule() -> None:
    usable, none = {"confidence": "low"}, {"confidence": "none", "fit": {"reason": "too few anchors"}}
    assert v4_shadow.keep_previous_model(usable, none)
    assert not v4_shadow.keep_previous_model(none, none)
    assert not v4_shadow.keep_previous_model(None, none)
    assert not v4_shadow.keep_previous_model(usable, {"confidence": "ok"})
    rec = v4_shadow.unusable_fit_record(none, at="x", reason="nightly", kept={"fitted_at": "2026-11-09T03:15:00+00:00"})
    assert (
        v4_shadow.describe_unusable_fit(rec)
        == "last refit unusable (too few anchors); keeping the model from 2026-11-09"
    )
