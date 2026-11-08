"""The coordinator passes its facts in at the decision call; the engine keeps none (Issue #1443, folds in #1440).

Occupant view: a nat-vent start, the soft start, the Away/Vacation TOU bank and the remote-sensor swing act on the
forecast, thermal model, today's outdoor peak and TOU window the coordinator holds when it asks. A copy written once
and never refreshed (or a snapshot reused across cycles) would run the house on stale inputs, e.g. veto a window-open
fan session on yesterday's forecast peak.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util

from custom_components.climate_advisor.automation import NAT_VENT_START_WINDOW_OPEN, AutomationEngine
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.engine_inputs import (
    EMPTY_ENGINE_INPUTS,
    TOU_AV_INACTIVE,
    CoordinatorEngineInputs,
    EngineFacts,
    StaticEngineInputs,
    TouAvWindow,
    decide,
    decide_sync,
    forecast_peak_next_2h,
)
from tests.test_restore_resolved_band import _make_engine

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _hourly(*temps: float, start: datetime = NOW) -> list[dict]:
    return [{"datetime": (start + timedelta(hours=h)).isoformat(), "temperature": t} for h, t in enumerate(temps)]


class _Coord:
    """The coordinator attributes engine_facts reads, with the coordinator's own engine_facts builder."""

    engine_facts = ClimateAdvisorCoordinator.engine_facts

    def __init__(self) -> None:
        self._thermal_model: dict = {}
        self._hourly_forecast_temps: list[dict] = []
        self._outdoor_temp_history: list[tuple[str, float]] = []
        self._tou_av_window = TOU_AV_INACTIVE


class _RaisingSource:
    """A facts source that fails the test if the engine builds facts itself (a live lookup)."""

    def __init__(self, thermal: dict | None = None) -> None:
        self.thermal = thermal or {}

    def build_facts(self, now):  # noqa: ANN001
        raise AssertionError("the engine looked a fact up instead of using the one passed in")

    def thermal_model(self) -> dict:
        return self.thermal


# --------------------------------------------------------------------- the pure peak (moved verbatim)
class TestForecastPeakNext2h:
    def test_empty_or_none(self):
        assert forecast_peak_next_2h(None, NOW) is None
        assert forecast_peak_next_2h([], NOW) is None

    def test_window_is_open_at_now_closed_at_two_hours(self):
        # hour 0 (== now) excluded, hours 1 and 2 included, hour 3 excluded
        assert forecast_peak_next_2h(_hourly(99, 70, 81, 95), NOW) == 81

    def test_naive_now_and_naive_entries_read_as_utc(self):
        naive = [{"datetime": "2026-07-01T13:00:00", "temperature": 77}]
        assert forecast_peak_next_2h(naive, NOW.replace(tzinfo=None)) == 77

    def test_bad_entries_skipped(self):
        rows = [{"datetime": None, "temperature": 99}, {"datetime": "junk", "temperature": 98}]
        rows += [{"datetime": (NOW + timedelta(hours=1)).isoformat(), "temperature": None}]
        assert forecast_peak_next_2h(rows, NOW) is None


# --------------------------------------------------------------------- the coordinator computes a frozen snapshot
class TestCoordinatorEngineFacts:
    def test_snapshot_holds_the_values_at_the_call(self):
        c = _Coord()
        c._hourly_forecast_temps = _hourly(60, 70, 72)
        c._outdoor_temp_history = [("t1", 70.0), ("t2", 84.5), ("t3", 80.0)]
        c._tou_av_window = TouAvWindow(expansion_f=2.0, schedule_id="peak", precondition_target=71.0)
        f = c.engine_facts(NOW)
        assert (f.as_of, f.forecast_peak_2h, f.outdoor_today_peak, f.outdoor_today_sample_count) == (NOW, 72, 84.5, 3)
        assert f.tou_av.schedule_id == "peak"

    def test_a_snapshot_is_frozen_not_a_live_view(self):
        c = _Coord()
        c._hourly_forecast_temps = _hourly(60, 70, 72)
        f = c.engine_facts(NOW)
        c._hourly_forecast_temps = _hourly(60, 90, 91)  # a later refresh does not reach a snapshot already passed
        assert f.forecast_peak_2h == 72
        with pytest.raises(dataclasses.FrozenInstanceError):
            f.forecast_peak_2h = 1.0  # type: ignore[misc]

    def test_each_call_builds_a_new_snapshot(self):
        """Catches a cached snapshot reused across cycles (forecast refreshed, buffer grown or cleared)."""
        c = _Coord()
        c._hourly_forecast_temps = _hourly(60, 70, 72)
        c._outdoor_temp_history = [("a", 70.0)]
        assert c.engine_facts(NOW).forecast_peak_2h == 72
        assert c.engine_facts(NOW).outdoor_today_peak == 70.0
        c._hourly_forecast_temps = _hourly(60, 88, 90)
        c._outdoor_temp_history.append(("b", 79.0))
        assert c.engine_facts(NOW).forecast_peak_2h == 90
        assert (c.engine_facts(NOW).outdoor_today_peak, c.engine_facts(NOW).outdoor_today_sample_count) == (79.0, 2)
        c._outdoor_temp_history.clear()  # the midnight reset
        assert (c.engine_facts(NOW).outdoor_today_peak, c.engine_facts(NOW).outdoor_today_sample_count) == (None, 0)
        c._tou_av_window = TOU_AV_INACTIVE
        assert c.engine_facts(NOW).tou_av is TOU_AV_INACTIVE

    def test_coordinator_mid_init_has_nothing_yet(self):
        f = CoordinatorEngineInputs(object()).build_facts(NOW)
        assert (f.forecast_peak_2h, f.outdoor_today_peak, f.outdoor_today_sample_count) == (None, None, 0)
        assert f.tou_av is TOU_AV_INACTIVE
        assert CoordinatorEngineInputs(object()).thermal_model() == {}

    def test_source_builds_through_the_coordinators_builder(self):
        c = _Coord()
        c._hourly_forecast_temps = _hourly(60, 77)
        assert CoordinatorEngineInputs(c).build_facts(NOW) == c.engine_facts(NOW)
        with pytest.raises(AttributeError):
            CoordinatorEngineInputs(c).thermal = {}  # type: ignore[attr-defined]


def test_static_and_empty_defaults():
    s = StaticEngineInputs()
    f = s.build_facts(NOW)
    assert s.thermal_model() == {} and f.forecast_peak_2h is None
    assert (f.outdoor_today_peak, f.outdoor_today_sample_count) == (None, 0)
    assert f.tou_av is TOU_AV_INACTIVE
    s.hourly_forecast = _hourly(60, 75)
    assert s.build_facts(NOW).forecast_peak_2h == 75
    assert EMPTY_ENGINE_INPUTS.thermal_model() == {}
    assert EMPTY_ENGINE_INPUTS.build_facts(NOW) == EngineFacts(as_of=NOW)
    with pytest.raises(AttributeError):
        EMPTY_ENGINE_INPUTS.thermal = {}  # type: ignore[attr-defined]


# --------------------------------------------------------------------- the engine holds no copy, uses what is passed
_REMOVED = (
    "_hourly_forecast_temps",
    "_thermal_model",
    "_outdoor_temp_today_peak",
    "_outdoor_temp_today_sample_count",
    "_tou_av_expansion_f",
    "_tou_av_active_schedule_id",
    "_tou_av_precondition_target",
)


def test_engine_has_no_copies():
    engine = _make_engine(indoor=72.0)
    for attr in _REMOVED:
        assert not hasattr(engine, attr), attr


def test_engine_built_via_new_reads_empty_inputs():
    engine = object.__new__(AutomationEngine)
    assert engine.inputs is EMPTY_ENGINE_INPUTS
    assert engine._control_model() == {}


def test_the_veto_gets_the_peak_passed_in_not_a_lookup():
    """#1138: forecast_peak_2h reaches decide_nat_vent_start_veto as the value the coordinator passed in."""
    engine = _make_engine(indoor=72.0)
    engine._inputs = _RaisingSource()
    with engine.decision_facts(EngineFacts(as_of=NOW, forecast_peak_2h=91.0)):
        veto = engine._build_nat_vent_start_veto_inputs(now=NOW, indoor=75.0, outdoor=65.0)
    assert veto.forecast_peak_2h == 91.0


def test_passed_facts_end_with_the_call():
    engine = _make_engine(indoor=72.0)
    engine.inputs.hourly_forecast = _hourly(60, 70)
    with engine.decision_facts(EngineFacts(as_of=NOW, forecast_peak_2h=91.0)):
        assert engine._facts(NOW).forecast_peak_2h == 91.0
    assert engine._facts(NOW).forecast_peak_2h == 70  # nothing kept on the engine: the source builds afresh


def test_a_stale_passed_snapshot_is_not_used():
    """A snapshot is for one decision: a timer that fires later inside the captured context gets fresh facts."""
    engine = _make_engine(indoor=72.0)
    engine.inputs.hourly_forecast = _hourly(60, 70, start=NOW)
    old = EngineFacts(as_of=NOW - timedelta(minutes=5), forecast_peak_2h=99.0)
    with engine.decision_facts(old):
        assert engine._facts(NOW).forecast_peak_2h == 70
    assert old.fresh_for(NOW - timedelta(minutes=5) + timedelta(seconds=59))
    assert not old.fresh_for(NOW)


def test_another_engines_facts_are_not_used():
    a, b = _make_engine(indoor=72.0), _make_engine(indoor=72.0)
    b.inputs.hourly_forecast = _hourly(60, 70)
    with a.decision_facts(EngineFacts(as_of=NOW, forecast_peak_2h=91.0)):
        assert b._facts(NOW).forecast_peak_2h == 70


def test_soft_start_and_tou_read_the_passed_snapshot():
    engine = _make_engine(indoor=72.0)
    engine._inputs = _RaisingSource()
    facts = EngineFacts(
        as_of=NOW,
        outdoor_today_peak=85.0,
        outdoor_today_sample_count=20,
        tou_av=TouAvWindow(expansion_f=3.0, schedule_id="s1", precondition_target=70.0),
    )
    with (
        patch("custom_components.climate_advisor.automation.dt_util.now", return_value=NOW),
        engine.decision_facts(facts),
    ):
        assert engine._active_tou_av_window().expansion_f == 3.0
        inputs = engine._build_nat_vent_fsm_inputs(now=NOW, indoor=75.0, outdoor=70.0)
    assert (inputs.outdoor_today_peak, inputs.outdoor_sample_count) == (85.0, 20)


def test_control_model_and_swing_read_the_model_accessor():
    engine = _make_engine(indoor=72.0)
    engine.inputs.thermal = {"confidence": "high", "k_passive": -0.3, "swing_cool_f": 1.2}
    assert engine._control_model()["k_passive"] == -0.3
    assert engine._remote_swing_f("cool") == 1.2
    assert engine._model_confidence_for_log() == "high"
    engine.inputs.thermal = {"confidence": "low", "k_passive": -0.1}
    assert engine._control_model()["k_passive"] == -0.1
    assert engine._model_confidence_for_log() == "low"


def test_control_model_provider_gets_the_owners_model_as_base():
    engine = _make_engine(indoor=72.0)
    seen: list = []
    engine._control_model_provider = lambda base: seen.append(base) or {"confidence": "v4"}
    engine.inputs.thermal = {"confidence": "high"}
    assert engine._control_model() == {"confidence": "v4"}
    assert seen == [{"confidence": "high"}]


# --------------------------------------------------------------------- the coordinator passes its facts in
def _coordinator():
    from tools.sim_harness.build_coordinator import build_headless_coordinator  # noqa: PLC0415

    coord, *_ = build_headless_coordinator()
    return coord


def test_coordinator_installs_the_source_and_owns_the_facts():
    coord = _coordinator()
    ae = coord.automation_engine
    assert isinstance(ae.inputs, CoordinatorEngineInputs)
    coord._thermal_model = {"confidence": "medium", "k_passive": -0.25}
    assert ae.inputs.thermal_model()["confidence"] == "medium"
    for attr in _REMOVED:
        assert not hasattr(ae, attr), attr


def test_a_coordinator_decision_reads_only_the_facts_it_passed_in():
    """No live read: inside engine_inputs.decide the engine never asks its source for a fact."""
    coord = _coordinator()
    ae = coord.automation_engine
    coord._hourly_forecast_temps = _hourly(60, 88, 90, start=dt_util.now())
    coord._outdoor_temp_history = [("a", 70.0), ("b", 77.0)]
    ae._inputs = _RaisingSource()
    seen: list = []
    real = ae._build_nat_vent_start_veto_inputs

    def _spy(**kw):
        out = real(**kw)
        seen.append(out.forecast_peak_2h)
        return out

    ae._build_nat_vent_start_veto_inputs = _spy
    decide_sync(coord, ae._nat_vent_start_decision, NAT_VENT_START_WINDOW_OPEN, indoor=75.0, outdoor=65.0)
    assert seen == [90]
    ts = dt_util.now() + timedelta(hours=1)  # the "Next Automation" prediction: facts as of the asked time
    decide_sync(
        coord, ae._nat_vent_start_decision, NAT_VENT_START_WINDOW_OPEN, indoor=75.0, outdoor=65.0, at=ts, facts_at=ts
    )
    assert seen[-1] == forecast_peak_next_2h(coord._hourly_forecast_temps, ts)
    asyncio.run(decide(coord, ae.check_natural_vent_conditions))  # a whole async entry point, no lookup either


def test_recorded_outdoor_reading_reaches_the_soft_start_peak():
    """#540: a recorded outdoor reading is in the next snapshot the coordinator passes in; no mirror write."""
    coord = _coordinator()
    now = dt_util.now()
    coord._apply_outdoor_temp(71.0, record_history=True)
    assert coord.engine_facts(now).outdoor_today_peak == 71.0  # read before the later readings arrive
    coord._apply_outdoor_temp(79.0, record_history=True)
    coord._apply_outdoor_temp(75.0, record_history=True)
    coord._apply_outdoor_temp(90.0, record_history=False)  # a live reading not recorded: not a sample
    facts = coord.engine_facts(now)
    assert (facts.outdoor_today_peak, facts.outdoor_today_sample_count) == (79.0, 3)
    assert coord.automation_engine._facts(now).outdoor_today_peak == 79.0  # an engine timer builds the same


# --------------------------------------------------------------------- engine-owned writes go through methods
def test_note_methods_write_the_engines_own_state():
    engine = _make_engine(indoor=72.0)
    engine.note_equipment_ran("heat", NOW)
    engine.note_equipment_ran("cool", NOW + timedelta(minutes=1))
    assert engine._last_hvac_heating_active == NOW.isoformat()
    assert engine._last_hvac_cooling_active == (NOW + timedelta(minutes=1)).isoformat()

    later = NOW + timedelta(hours=1)
    engine._fan_active = True
    engine._natural_vent_active = False
    engine.note_cycle_activity("Heating", later)
    assert engine._last_hvac_heating_active == later.isoformat()
    assert engine._last_fan_active == later.isoformat()
    assert engine._last_natvent_active is None
    engine.note_cycle_activity("idle", later + timedelta(hours=1))
    assert engine._last_hvac_heating_active == later.isoformat()  # idle stamps no compressor


def test_fan_and_lifecycle_methods():
    engine = _make_engine(indoor=72.0)
    engine._fan_active, engine._fan_on_since = True, NOW.isoformat()
    engine.clear_stale_hvac_fan_flag()
    assert engine._fan_active is False and engine._fan_on_since == NOW.isoformat()
    engine._fan_active = True
    engine.clear_stale_fan_ownership()
    assert engine._fan_active is False and engine._fan_on_since is None
    engine.note_observed_fan_origin("post_heat_tail")
    assert engine._observed_fan_origin_hint == "post_heat_tail"
    assert engine._shut_down is False and engine._ha_stopping is False
    engine.mark_ha_stopping()
    engine.latch_shutdown()
    assert engine._shut_down is True and engine._ha_stopping is True
