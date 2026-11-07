"""Prediction-engine registry and the primary/shadow settings (Issue #1230).

Occupant view: Settings -> Prediction engine picks the engine behind the Status chart's main line (v4 by default) and an
optional second one for comparison. At most those two run; a new engine (v5) is picked up by registering it. While the
primary has not finished learning, the chart says so and shows the schedule estimate, never the other engine.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor import prediction_engines as pe
from custom_components.climate_advisor.const import (
    CONFIG_METADATA,
    DEFAULT_PRIMARY_ENGINE,
    DEFAULT_SHADOW_ENGINE,
    ENGINE_NONE,
)
from tests.test_config_flow import FULL_CONFIG, _make_options_flow

NOW = datetime(2026, 11, 6, 20, 0, tzinfo=UTC)


class FakeV5:
    """A fifth engine: registering it is all it takes."""

    id = "v5"
    label = "v5 (fake)"
    state_file = "climate_advisor_v5_model.json"
    archive_file = "climate_advisor_v5_archive.json"

    def fit(self, history):
        return {"confidence": "high", "history_rows": len(history.get("rows", []))}

    def predict(self, state, horizon_hours, plan_inputs):
        return {"_ca": [(NOW + timedelta(hours=h), 70.0 + h) for h in range(1, int(horizon_hours) + 1)]}

    def confidence(self, state):
        return state.get("confidence", "none") if isinstance(state, dict) else "none"

    def state_of(self, source):
        return getattr(source, "_v5_state", None)

    def future_path(self, source):
        return self.predict(self.state_of(source), 3, {})

    def past(self, source, sensor, lead, start, end):
        return {int(start): 71.5} if sensor == "_ca" and lead == "4h" else {}


@pytest.fixture
def v5():
    engine = pe.register(FakeV5())
    yield engine
    pe.unregister("v5")


# ---------------------------------------------------------------------------------------------- registry
def test_v3_and_v4_are_registered_with_the_contract() -> None:
    assert list(pe.ENGINES) == ["v3", "v4"]
    for engine in pe.ENGINES.values():
        assert isinstance(engine, pe.PredictionEngine)
        assert engine.label and engine.state_file and engine.archive_file
    assert pe.ENGINES["v4"].state_file == "climate_advisor_v4_model.json"
    assert pe.ENGINES["v4"].archive_file == "climate_advisor_v4_archive.json"


def test_v3_adapter_reads_the_learning_state_and_the_logged_archive() -> None:
    v3 = pe.ENGINES["v3"]
    src = SimpleNamespace(
        automation_engine=SimpleNamespace(_thermal_model={"confidence": "medium"}),
        _last_predicted_indoor=[{"ts": NOW.isoformat(), "temp": 70.5}],
        _pred_archive={1800: 69.0, 99999999999: 1.0},
    )
    assert v3.confidence(v3.state_of(src)) == "medium" and pe.usable(v3, src)
    assert v3.future_path(src)["_ca"][0][1] == 70.5
    assert v3.past(src, "_ca", "4h", 0, 3600) == {1800: 69.0} and v3.past(src, "_ca", "1h", 0, 3600) == {}
    built = []
    out = v3.predict(
        {"confidence": "low"},
        24,
        {"build_predicted_indoor": lambda **kw: built.append(kw) or [{"ts": NOW.isoformat(), "temp": 68.0}]},
    )
    assert built[0]["thermal_model"] == {"confidence": "low"} and out["_ca"][0][1] == 68.0


def test_v4_adapter_reads_the_model_store_the_archive_and_the_ca_alias() -> None:
    v4 = pe.ENGINES["v4"]
    archive = SimpleNamespace(series=lambda sensor, lead, start, end: {1800: 66.0})
    src = SimpleNamespace(
        _v4_store=SimpleNamespace(v4_model={"confidence": "none", "primary": "climate.hall"}),
        _v4_archive=archive,
        _v4_last_future={"predictions": {"climate.hall": [(NOW, 67.0)]}},
    )
    assert v4.confidence(v4.state_of(src)) == "none" and not pe.usable(v4, src)  # a stored "none" is still learning
    assert v4.future_path(src)["_ca"] == [(NOW, 67.0)]  # the CA line follows the model's primary sensor
    assert v4.past(src, "climate.hall", "4h", 0, 3600) == {1800: 66.0}
    src._v4_store.v4_model["confidence"] = "medium"
    assert pe.usable(v4, src)


# ---------------------------------------------------------------------------------------------- settings
def test_defaults_are_v4_primary_and_no_shadow_for_upgrades_and_new_installs() -> None:
    assert DEFAULT_PRIMARY_ENGINE == "v4" and DEFAULT_SHADOW_ENGINE == ENGINE_NONE
    assert CONFIG_METADATA["primary_engine"]["default"] == "v4"
    assert CONFIG_METADATA["shadow_engine"]["default"] == "none"
    assert pe.primary_id({}) == "v4" and pe.shadow_id({}) == "none"  # no migration: read with defaults
    assert pe.primary_id(None) == "v4"


def test_unknown_values_fall_back_with_a_log_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    pe._warned.clear()
    assert pe.primary_id({"primary_engine": "v9"}) == "v4"
    assert pe.shadow_id({"shadow_engine": "v9"}) == "none"
    assert pe.shadow_id({"primary_engine": "v3", "shadow_engine": "v3"}) == "none"
    text = " ".join(r.getMessage() for r in caplog.records)
    assert (
        "Unknown primary_engine 'v9'" in text and "Unknown shadow_engine 'v9'" in text and "equals the primary" in text
    )


def test_at_most_two_engines_run_and_v3_runs_while_it_drives_control() -> None:
    assert pe.running_ids({}) == ["v4", "v3"]  # v4 primary; v3 for control
    assert pe.running_ids({"primary_engine": "v3"}) == ["v3"]  # v4 is off: not fitted, predicted or archived
    assert pe.is_running({"primary_engine": "v3"}, "v4") is False
    cfg = {"primary_engine": "v3", "shadow_engine": "v4"}
    assert pe.running_ids(cfg) == ["v3", "v4"] and pe.is_running(cfg, "v4")


def test_the_options_form_validates_shadow_against_primary() -> None:
    assert pe.validate("v4", "none") is None and pe.validate("v4", "v3") is None
    assert pe.validate("v4", "v4") == "shadow_same_as_primary"
    assert pe.validate("v9", "none") == "unknown_engine" and pe.validate("v4", "v9") == "unknown_engine"
    base = dict(FULL_CONFIG)
    flow, captured = _make_options_flow(base)
    form = asyncio.run(flow.async_step_advanced({**_advanced_input(), "primary_engine": "v4", "shadow_engine": "v4"}))
    assert form["errors"] == {"shadow_engine": "shadow_same_as_primary"} and "data" not in captured
    flow, captured = _make_options_flow(base)
    asyncio.run(flow.async_step_advanced({**_advanced_input(), "primary_engine": "v3", "shadow_engine": "v4"}))
    assert captured["data"]["primary_engine"] == "v3" and captured["data"]["shadow_engine"] == "v4"


def _advanced_input() -> dict:
    return {
        "learning_enabled": True,
        "adaptive_preheat_enabled": True,
        "adaptive_setback_enabled": True,
        "weather_bias_enabled": True,
        "min_preheat_minutes": 30,
        "max_preheat_minutes": 240,
        "default_preheat_minutes": 120,
        "default_tou_lead_minutes": 45,
        "preheat_safety_margin": 1.3,
        "max_setback_depth_f": 4,
        "aggressive_savings": False,
        "comfort_mode_switch_min_interval_s": 600,
    }


def test_strings_and_translations_cover_the_new_fields_and_errors() -> None:
    root = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"
    for rel in ("strings.json", "translations/en.json"):
        data = json.loads((root / rel).read_text(encoding="utf-8"))
        step = data["options"]["step"]["advanced"]
        for key in ("primary_engine", "shadow_engine"):
            assert step["data"][key] and step["data_description"][key]
        assert "shadow_same_as_primary" in data["options"]["error"] and "unknown_engine" in data["options"]["error"]


# ---------------------------------------------------------------------------------------------- a fifth engine
def test_a_registered_v5_is_picked_up_by_settings_dashboard_and_archive(v5) -> None:
    # settings: the dropdowns come from the registry, validation accepts it, the form saves it
    assert ("v5", "v5 (fake)") in pe.options(False) and ("v5", "v5 (fake)") in pe.options(True)
    assert pe.options(True)[0] == ("none", "None")
    assert pe.validate("v5", "v4") is None and pe.validate("v5", "v5") == "shadow_same_as_primary"
    flow, captured = _make_options_flow(dict(FULL_CONFIG))
    asyncio.run(flow.async_step_advanced({**_advanced_input(), "primary_engine": "v5", "shadow_engine": "none"}))
    assert captured["data"]["primary_engine"] == "v5"
    # still at most two (plus v3 for control): v4 is off when v5 is primary and nothing else is chosen
    cfg = {"primary_engine": "v5", "shadow_engine": "v4"}
    assert pe.running_ids(cfg) == ["v5", "v4", "v3"] and pe.is_running({"primary_engine": "v5"}, "v4") is False
    # dashboard: the engine block lists it, flags it primary, and reports its state
    src = SimpleNamespace(_v5_state={"confidence": "high"}, _v4_fit_running=False)
    block = pe.engine_state(src, {"primary_engine": "v5", "shadow_engine": "v3"})
    ids = [e["id"] for e in block["engines"]]
    assert ids == ["v3", "v4", "v5"] and block["primary"] == "v5" and block["shadow"] == "v3"
    row = next(e for e in block["engines"] if e["id"] == "v5")
    assert row["primary"] and row["running"] and row["usable"] and row["confidence"] == "high"
    assert block["fallback"] is False
    assert next(e for e in block["engines"] if e["id"] == "v4")["running"] is False
    # archive: its own past values are read through the registry
    assert pe.ENGINES["v5"].past(src, "_ca", "4h", 1800.0, 3600.0) == {1800: 71.5}
    # fit/predict contract
    state = pe.ENGINES["v5"].fit({"rows": [1, 2, 3]})
    assert state["history_rows"] == 3 and len(pe.ENGINES["v5"].predict(state, 6, {})["_ca"]) == 6
    # an unregistered v5 reads as unknown again (back to the default)
    pe.unregister("v5")
    pe._warned.clear()
    assert pe.primary_id({"primary_engine": "v5"}) == "v4"
    pe.register(v5)  # leave the fixture's teardown balanced


# ---------------------------------------------------------------------------------------------- display state
def test_engine_state_primary_learning_then_ready_and_text() -> None:
    src = SimpleNamespace(
        _v4_store=SimpleNamespace(v4_model=None),
        _v4_fit_running=False,
        automation_engine=SimpleNamespace(_thermal_model={"confidence": "medium"}),
    )
    s = pe.engine_state(src, {})
    assert s["fallback"] and s["primary_state"] == "learning"
    assert s["text"] == "Prediction: learning — showing the schedule estimate"
    src._v4_fit_running = True
    assert pe.engine_state(src, {})["text"] == "Learning: v4 is fitting from your history"
    src._v4_store.v4_model = {"confidence": "low"}
    s = pe.engine_state(src, {})
    assert not s["fallback"] and s["text"] == "" and s["primary_state"] == "ready"
    # v3 as primary with a mature model is ready; with no thermal model it is learning (its own ramp fallback)
    assert pe.engine_state(src, {"primary_engine": "v3"})["primary_state"] == "ready"
    src.automation_engine._thermal_model = {}
    assert pe.engine_state(src, {"primary_engine": "v3"})["fallback"] is True
    # the other engine is never substituted: v3 being mature does not make a learning v4 "ready"
    src._v4_store.v4_model = None
    assert pe.engine_state(src, {"primary_engine": "v4"})["fallback"] is True
