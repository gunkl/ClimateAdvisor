"""The engine reads the coordinator-owned facts live through one read-only provider (Issue #1443, folds in #1440).

Occupant view: a nat-vent start, the soft start, the Away/Vacation TOU bank and the remote-sensor swing act on the
forecast, thermal model, today's outdoor peak and TOU window the coordinator holds NOW. A copy written once and never
refreshed would run the house on stale inputs (e.g. veto a window-open fan session on yesterday's forecast peak).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.engine_inputs import (
    EMPTY_ENGINE_INPUTS,
    TOU_AV_INACTIVE,
    CoordinatorEngineInputs,
    StaticEngineInputs,
    TouAvWindow,
    forecast_peak_next_2h,
)
from tests.test_restore_resolved_band import _make_engine

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _hourly(*temps: float, start: datetime = NOW) -> list[dict]:
    return [{"datetime": (start + timedelta(hours=h)).isoformat(), "temperature": t} for h, t in enumerate(temps)]


class _Coord:
    """The four coordinator attributes CoordinatorEngineInputs reads."""

    def __init__(self) -> None:
        self._thermal_model: dict = {}
        self._hourly_forecast_temps: list[dict] = []
        self._outdoor_temp_history: list[tuple[str, float]] = []
        self._tou_av_window = TOU_AV_INACTIVE


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


# --------------------------------------------------------------------- the production provider reads live
class TestCoordinatorEngineInputs:
    def test_reads_the_coordinators_current_forecast_not_a_copy(self):
        c = _Coord()
        inputs = CoordinatorEngineInputs(c)
        c._hourly_forecast_temps = _hourly(60, 70, 72)
        assert inputs.forecast_peak_next_2h(NOW) == 72
        c._hourly_forecast_temps = _hourly(60, 88, 90)  # a refresh REPLACES the list
        assert inputs.forecast_peak_next_2h(NOW) == 90
        c._hourly_forecast_temps = []  # an outage with nothing kept
        assert inputs.forecast_peak_next_2h(NOW) is None

    def test_thermal_model_replaced(self):
        c = _Coord()
        inputs = CoordinatorEngineInputs(c)
        assert inputs.thermal_model() == {}
        c._thermal_model = {"confidence": "high", "k_passive": -0.2}
        assert inputs.thermal_model()["confidence"] == "high"
        c._thermal_model = {"confidence": "low"}
        assert inputs.thermal_model() == {"confidence": "low"}

    def test_outdoor_peak_follows_the_buffer(self):
        c = _Coord()
        inputs = CoordinatorEngineInputs(c)
        assert (inputs.outdoor_today_peak(), inputs.outdoor_today_sample_count()) == (None, 0)
        c._outdoor_temp_history.append(("t1", 70.0))
        c._outdoor_temp_history.append(("t2", 84.5))
        c._outdoor_temp_history.append(("t3", 80.0))
        assert (inputs.outdoor_today_peak(), inputs.outdoor_today_sample_count()) == (84.5, 3)
        c._outdoor_temp_history.clear()  # the midnight reset
        assert (inputs.outdoor_today_peak(), inputs.outdoor_today_sample_count()) == (None, 0)

    def test_tou_window(self):
        c = _Coord()
        inputs = CoordinatorEngineInputs(c)
        assert inputs.tou_av_window() is TOU_AV_INACTIVE
        c._tou_av_window = TouAvWindow(expansion_f=2.0, schedule_id="peak", precondition_target=71.0)
        assert inputs.tou_av_window().expansion_f == 2.0
        c._tou_av_window = TOU_AV_INACTIVE
        assert inputs.tou_av_window().schedule_id is None

    def test_coordinator_mid_init_has_nothing_yet(self):
        inputs = CoordinatorEngineInputs(object())
        assert inputs.thermal_model() == {}
        assert inputs.forecast_peak_next_2h(NOW) is None
        assert (inputs.outdoor_today_peak(), inputs.outdoor_today_sample_count()) == (None, 0)
        assert inputs.tou_av_window() is TOU_AV_INACTIVE

    def test_read_only(self):
        inputs = CoordinatorEngineInputs(_Coord())
        with pytest.raises(AttributeError):
            inputs.thermal = {}  # type: ignore[attr-defined]


def test_static_and_empty_defaults():
    s = StaticEngineInputs()
    assert s.thermal_model() == {} and s.forecast_peak_next_2h(NOW) is None
    assert (s.outdoor_today_peak(), s.outdoor_today_sample_count()) == (None, 0)
    assert s.tou_av_window() is TOU_AV_INACTIVE
    s.hourly_forecast = _hourly(60, 75)
    assert s.forecast_peak_next_2h(NOW) == 75
    assert EMPTY_ENGINE_INPUTS.thermal_model() == {} and EMPTY_ENGINE_INPUTS.tou_av_window() is TOU_AV_INACTIVE
    with pytest.raises(AttributeError):
        EMPTY_ENGINE_INPUTS.thermal = {}  # type: ignore[attr-defined]


# --------------------------------------------------------------------- the engine holds no copy, reads the provider
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


def _wired_engine() -> tuple[AutomationEngine, _Coord]:
    c = _Coord()
    base = _make_engine(indoor=72.0)
    base._inputs = CoordinatorEngineInputs(c)
    return base, c


def test_nat_vent_veto_inputs_read_the_current_forecast_peak():
    """#1138: the veto gets the peak as its forecast_peak_2h argument, computed from the owner's list at the call."""
    engine, c = _wired_engine()
    c._hourly_forecast_temps = _hourly(60, 70, 72)
    first = engine._build_nat_vent_start_veto_inputs(now=NOW, indoor=75.0, outdoor=65.0)
    c._hourly_forecast_temps = _hourly(60, 90, 91)
    second = engine._build_nat_vent_start_veto_inputs(now=NOW, indoor=75.0, outdoor=65.0)
    assert (first.forecast_peak_2h, second.forecast_peak_2h) == (72, 91)


def test_control_model_and_swing_read_the_current_model():
    engine, c = _wired_engine()
    c._thermal_model = {"confidence": "high", "k_passive": -0.3, "swing_cool_f": 1.2}
    assert engine._control_model()["k_passive"] == -0.3
    assert engine._remote_swing_f("cool") == 1.2
    assert engine._model_confidence_for_log() == "high"
    c._thermal_model = {"confidence": "low", "k_passive": -0.1}
    assert engine._control_model()["k_passive"] == -0.1
    assert engine._model_confidence_for_log() == "low"


def test_control_model_provider_gets_the_owners_model_as_base():
    engine, c = _wired_engine()
    seen: list = []
    engine._control_model_provider = lambda base: seen.append(base) or {"confidence": "v4"}
    c._thermal_model = {"confidence": "high"}
    assert engine._control_model() == {"confidence": "v4"}
    assert seen == [{"confidence": "high"}]


def test_tou_window_reads_the_owner():
    engine, c = _wired_engine()
    assert engine._active_tou_av_window() is TOU_AV_INACTIVE
    c._tou_av_window = TouAvWindow(expansion_f=3.0, schedule_id="s1", precondition_target=70.0)
    assert engine._active_tou_av_window().expansion_f == 3.0


# --------------------------------------------------------------------- the coordinator owns and installs
def test_coordinator_installs_the_provider_and_owns_the_facts():
    from tools.sim_harness.build_coordinator import build_headless_coordinator  # noqa: PLC0415

    coord, *_ = build_headless_coordinator()
    ae = coord.automation_engine
    assert isinstance(ae.inputs, CoordinatorEngineInputs)
    coord._thermal_model = {"confidence": "medium", "k_passive": -0.25}
    assert ae.inputs.thermal_model()["confidence"] == "medium"
    coord._hourly_forecast_temps = _hourly(60, 81, start=NOW)
    assert ae.inputs.forecast_peak_next_2h(NOW) == 81
    coord._outdoor_temp_history = [("a", 70.0), ("b", 77.0)]
    assert ae.inputs.outdoor_today_peak() == 77.0
    for attr in _REMOVED:
        assert not hasattr(ae, attr), attr


def test_recorded_outdoor_reading_reaches_the_soft_start_peak():
    """#540: a recorded outdoor reading is the soft start's peak/sample count with no mirror write."""
    from tools.sim_harness.build_coordinator import build_headless_coordinator  # noqa: PLC0415

    coord, *_ = build_headless_coordinator()
    coord._apply_outdoor_temp(71.0, record_history=True)
    assert coord.automation_engine.inputs.outdoor_today_peak() == 71.0  # read before the later readings arrive
    coord._apply_outdoor_temp(79.0, record_history=True)
    coord._apply_outdoor_temp(75.0, record_history=True)
    coord._apply_outdoor_temp(90.0, record_history=False)  # a live reading not recorded: not a sample
    assert coord.automation_engine.inputs.outdoor_today_peak() == 79.0
    assert coord.automation_engine.inputs.outdoor_today_sample_count() == 3


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
