"""Issue #1477 - the learned swing is rounded UP to the next half degree F, and fine readings decide.

Occupant framing: Zone 1's thermostat lets the hallway drift about 1.44 F below the setpoint before the heat
starts (its finer sensor reads it), but Climate Advisor showed and used 1.0 F: five older samples taken on the
thermostat's whole-degree display (1.0 each) out-voted the two fine 1.44 samples in the median. A swing that is
too small makes CA expect the heat or AC sooner than the thermostat really starts (the bedroom outer loop nudges
the setpoint up at 1.0 F short instead of 1.5 F, the comfort tolerance counts normal droop as a shortfall).
Owner decision 2026-11-08: store and use ``ceil(x * 2) / 2`` F (1.44 -> 1.5), heat and cool alike; a Celsius
install shows that delta x 5/9 (1.5 F -> 0.83 C).

These tests drive the real swing module, the real LearningEngine (load + record) and the real coordinator
thermostat listener (``tests.test_swing_hysteresis_1234._Rig``), plus ``tools/thermal_replay``.
"""

from __future__ import annotations

import importlib
import json
import logging
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from custom_components.climate_advisor import swing
from custom_components.climate_advisor.const import THERMAL_SWING_DEFAULT_F, THERMAL_SWING_ESTIMATOR_WINDOW
from custom_components.climate_advisor.learning import LearningEngine
from custom_components.climate_advisor.temperature import display_delta, format_temp_delta
from tests.test_swing_hysteresis_1234 import _MODES, _T0, _past, _Rig, _settled

# ---------------------------------------------------------------------------
# Rounding (owner rule) - degF, and the Celsius display of the same delta
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        (1.44, 1.5),  # Zone 1's fine heat swing
        (1.0, 1.0),  # already a half degree
        (1.0000001, 1.0),  # float noise must not round a whole half degree up
        (0.9999999, 1.0),
        (1.01, 1.5),
        (1.5, 1.5),  # the default stays the default
        (1.51, 2.0),
        (0.1, 0.5),  # THERMAL_SWING_MIN_F
        (2.26, 2.5),
        (5.0, 5.0),  # THERMAL_SWING_MAX_F
        (None, None),
    ],
)
def test_round_swing_up_to_the_next_half_degree_f(raw, stored):
    assert swing.round_swing_up_f(raw) == stored


def test_default_and_fallback_are_already_on_the_half_degree_grid():
    assert swing.round_swing_up_f(THERMAL_SWING_DEFAULT_F) == THERMAL_SWING_DEFAULT_F == 1.5
    tol, src = swing.swing_tolerance(None)
    assert (tol, src) == (1.5, "default")


@pytest.mark.parametrize(("stored_f", "celsius"), [(1.5, 0.83), (1.0, 0.56), (2.0, 1.11), (0.5, 0.28)])
def test_celsius_shows_the_same_delta_times_five_ninths_no_offset(stored_f, celsius):
    """A swing is a difference: 1.5 F is 0.83 C (never 1.5 F - 32 or (1.5 - 32) x 5/9)."""
    assert display_delta(stored_f, "celsius", 2) == celsius
    assert format_temp_delta(stored_f, "celsius", 2) == f"{celsius:.2f}°C"
    assert display_delta(stored_f, "fahrenheit", 2) == stored_f


# ---------------------------------------------------------------------------
# Fine vs coarse samples (the estimator)
# ---------------------------------------------------------------------------


def _s(i: int, value: float, res: float, sp: float = 69.0, mode: str = "heat", unit: str = "fahrenheit") -> dict:
    on = sp - value if mode == "heat" else sp + value
    return {
        "ts": f"2026-11-{1 + i // 24:02d}T{i % 24:02d}:10:00-08:00",
        "value_f": value,
        "resolution_f": res,
        "setpoint_f": sp,
        "on_indoor_f": on,
        "off_indoor_f": on + (0.36 if mode == "heat" else -0.36) if res == 0.0 else on,
        "reading_unit": unit,
        "off_point_f": None,
    }


def _zone1_window() -> list[dict]:
    """The live Zone 1 heat window on 2026-11-08: five whole-degree 1.0 samples, then two fine 1.44."""
    return [_s(i, 1.0, 1.0) for i in range(5)] + [_s(5, 1.44, 0.0, sp=68.0), _s(6, 1.44, 0.0, sp=68.0)]


def test_zone1_fine_samples_decide_and_round_up_to_one_and_a_half():
    summary = swing.summarize_swing_samples(_zone1_window())
    assert summary["raw_value_f"] == 1.44
    assert summary["value_f"] == 1.5
    assert (summary["n_used"], summary["n_window"]) == (2, 7)
    assert summary["resolution_f"] == 0.0


def test_before_1477_the_coarse_samples_outvoted_the_fine_ones():
    """Control: the plain median of the same window is 1.0 - the stuck value the owner saw."""
    from statistics import median

    assert median(s["value_f"] for s in _zone1_window()) == 1.0


def test_coarse_only_window_uses_every_sample():
    window = [_s(i, v, 1.0) for i, v in enumerate([1.0, 2.0, 1.0, 1.0, 2.0])]
    summary = swing.summarize_swing_samples(window)
    assert (summary["n_used"], summary["raw_value_f"], summary["value_f"]) == (5, 1.0, 1.0)


def test_half_degree_samples_beat_whole_degree_ones():
    window = [_s(0, 2.0, 1.0), _s(1, 2.0, 1.0), _s(2, 2.0, 1.0), _s(3, 1.5, 0.5), _s(4, 1.5, 0.5)]
    window[3]["on_indoor_f"], window[3]["off_indoor_f"] = 67.5, 68.0
    window[4]["on_indoor_f"], window[4]["off_indoor_f"] = 67.5, 68.5
    summary = swing.summarize_swing_samples(window)
    assert (summary["n_used"], summary["value_f"]) == (2, 1.5)


def test_a_finer_tier_whose_readings_are_whole_degree_is_not_finer():
    """A configured sensor that reports whole degrees is recorded as source-fine (0.0) but is not finer."""
    coarse = [_s(i, 1.0, 1.0) for i in range(3)]
    fake_fine = [_s(3 + i, 2.0, 0.0) for i in range(3)]
    for s, (on, off) in zip(fake_fine, [(67.0, 68.0), (67.0, 69.0), (67.0, 70.0)], strict=True):
        s["on_indoor_f"], s["off_indoor_f"] = on, off
    used = swing.finest_resolution_samples(coarse + fake_fine)
    assert len(used) == 6


def test_samples_without_a_resolution_count_as_coarse():
    legacy = {k: v for k, v in _s(0, 1.0, 1.0).items() if k != "resolution_f"}
    window = [legacy, legacy | {"ts": "2026-11-02T01:00:00-08:00"}, _s(9, 1.44, 0.0)]
    assert swing.summarize_swing_samples(window)["n_used"] == 1


def test_the_fine_rule_is_reversible_coarse_samples_return_once_the_fine_ones_roll_out():
    """Nothing is dropped: when the fine sensor goes away, the window refills with coarse samples and they decide."""
    cache: dict = {}
    swing.apply_swing_samples(cache, "heat", _zone1_window())
    assert cache["swing_heat_f"] == 1.5
    later = [_s(100 + i, 2.0, 1.0) for i in range(THERMAL_SWING_ESTIMATOR_WINDOW)]
    swing.apply_swing_samples(cache, "heat", later[: THERMAL_SWING_ESTIMATOR_WINDOW - 2])
    assert cache["swing_heat_f"] == 1.5  # the two fine samples are still in the window
    swing.apply_swing_samples(cache, "heat", later[THERMAL_SWING_ESTIMATOR_WINDOW - 2 :])
    assert len(cache["swing_heat_samples"]) == THERMAL_SWING_ESTIMATOR_WINDOW
    assert all(s["resolution_f"] == 1.0 for s in cache["swing_heat_samples"])
    assert cache["swing_heat_f"] == 2.0


# ---------------------------------------------------------------------------
# Learning: accept moves the value, the window rolls, the stored value is corrected on load
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["heat", "cool"])
def test_new_samples_move_the_value_and_the_window_rolls(tmp_path, mode):
    engine = LearningEngine(tmp_path)
    for i in range(THERMAL_SWING_ESTIMATOR_WINDOW):
        engine.record_swing_sample(mode, _s(i, 1.0, 0.0, mode=mode))
    model = engine.get_thermal_model()
    assert model[f"swing_{mode}_f"] == 1.0
    for i in range(THERMAL_SWING_ESTIMATOR_WINDOW):  # the thermostat's deadband was widened
        engine.record_swing_sample(mode, _s(50 + i, 1.8, 0.0, mode=mode))
        if i == THERMAL_SWING_ESTIMATOR_WINDOW // 2:
            assert engine.get_thermal_model()[f"swing_{mode}_f"] == 2.0  # the median has moved: 1.8 -> 2.0
    model = engine.get_thermal_model()
    assert model[f"swing_{mode}_f"] == 2.0 and model[f"swing_{mode}_raw_f"] == 1.8
    cache = engine._state.thermal_model_cache
    assert len(cache[f"swing_{mode}_samples"]) == THERMAL_SWING_ESTIMATOR_WINDOW
    assert cache[f"swing_{mode}_samples"][0]["value_f"] == 1.8  # the oldest 1.0 samples rolled out
    assert model[f"observation_count_swing_{mode}"] == 2 * THERMAL_SWING_ESTIMATOR_WINDOW


def _zone1_learning_file(tmp_path) -> None:
    cache = {
        "swing_definition_version": 2,
        "swing_heat_f": 1.0,
        "swing_cool_f": None,
        "observation_count_swing_heat": 7,
        "observation_count_swing_cool": 0,
        "swing_heat_samples": _zone1_window(),
        "swing_heat_resolution_f": 0.0,
        "swing_heat_off_point_f": 1.0,
        "swing_heat_reject_counts": {"setpoint_change": 14},
    }
    LearningEngine(tmp_path)._db_path.write_text(json.dumps({"thermal_model_cache": cache}))


def test_a_stored_value_from_the_old_estimator_is_corrected_on_load(tmp_path, caplog):
    _zone1_learning_file(tmp_path)
    engine = LearningEngine(tmp_path)
    with caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.learning"):
        engine.load_state()
    model = engine.get_thermal_model()
    assert model["swing_heat_f"] == 1.5 and model["swing_heat_raw_f"] == 1.44
    assert model["swing_cool_f"] is None  # cool has nothing to learn from yet
    assert model["observation_count_swing_heat"] == 7  # counts and rejects untouched
    assert engine._state.thermal_model_cache["swing_heat_reject_counts"] == {"setpoint_change": 14}
    assert any("heat swing recomputed 1.0 -> 1.5" in r.getMessage() for r in caplog.records)


def test_refresh_is_idempotent_and_rounds_a_value_without_samples():
    cache = {"swing_heat_f": 1.2, "swing_cool_f": 1.5, "swing_definition_version": 2}
    assert swing.refresh_swing_cache(cache) == ["heat"]
    assert cache["swing_heat_f"] == 1.5 and cache["swing_cool_f"] == 1.5
    assert swing.refresh_swing_cache(cache) == []


# ---------------------------------------------------------------------------
# Through the real thermostat listener: fine sensor after whole-degree history, cool, heat/cool range
# ---------------------------------------------------------------------------


def test_listener_zone1_story_whole_degree_history_then_finer_sensor(tmp_path):
    """The live path end to end: five whole-degree cycles (climate_fallback), then two on the finer sensor."""
    rig = _Rig(tmp_path, "heat", source="climate")
    sp = 69.0
    _settled(rig, sp)
    t = _T0
    for _ in range(5):
        rig.cycle(t, sp=sp, on_indoor=68.0, off_indoor=68.0)
        t += timedelta(hours=2)
    assert (rig.swing, rig.count) == (1.0, 5)
    rig.coord.config["indoor_temp_source"] = "sensor"  # the finer sensor now supplies the reading
    for _ in range(2):
        rig.cycle(t, sp=sp, on_indoor=sp - 1.44, off_indoor=sp - 1.08)
        t += timedelta(hours=2)
    assert rig.count == 7
    assert rig.raw == pytest.approx(1.44)
    assert rig.swing == 1.5


def test_listener_rejects_a_start_seconds_after_ca_raised_the_setpoint(tmp_path):
    """Zone 1's 14 rejects: the bedroom loop (or wake-up, or the occupant) raised the setpoint and the heat
    started 2-4 s later because the room was now below it - a commanded start, not the thermostat's drift."""
    rig = _Rig(tmp_path, "heat")
    _settled(rig, 66.0)
    rig.fire(_T0, setpoint=70.0, indoor=66.74)  # CA's remote-offset write: +4 F
    rig.fire(_T0 + timedelta(seconds=2.11), action="heating")
    rig.fire(_T0 + timedelta(minutes=8), action="idle", indoor=67.28)
    audit = rig.model()["swing_debug"]["heat"]["recent"]
    assert [r["reason"] for r in audit] == ["setpoint_change"] and audit[0]["setpoint_age_s"] == pytest.approx(2.11)
    assert (rig.swing, rig.count) == (None, 0)
    # ... and the thermostat's own drift-start later, with the setpoint steady, IS learned
    rig.cycle(_T0 + timedelta(hours=1), sp=70.0, on_indoor=68.56, off_indoor=68.92)
    assert rig.count == 1 and rig.raw == pytest.approx(1.44) and rig.swing == 1.5


def test_listener_learns_cool_when_cooling_runs(tmp_path):
    """Cool never learned on Zone 1 only because it has not cooled since swing logging began."""
    rig = _Rig(tmp_path, "cool")
    sp = 74.0
    _settled(rig, sp)
    assert rig.swing is None
    rig.cycle(_T0, sp=sp, on_indoor=_past("cool", sp, 1.26), off_indoor=_past("cool", sp, 0.6))
    rig.cycle(_T0 + timedelta(hours=2), sp=sp, on_indoor=_past("cool", sp, 1.26), off_indoor=_past("cool", sp, 0.4))
    assert rig.count == 2 and rig.raw == pytest.approx(1.26) and rig.swing == 1.5
    assert rig.model()["confidence_swing_cool"] == "low"


class _RangeRig(_Rig):
    """A heat/cool-range thermostat: no single ``temperature``, only ``target_temp_low`` / ``target_temp_high``."""

    def __init__(self, tmp_path, mode: str) -> None:
        super().__init__(tmp_path, mode)
        self.state_mode = "heat_cool"
        self.low, self.high = 68.0, 74.0

    def _state(self, action, setpoint, indoor_attr, state_mode):
        s = MagicMock()
        s.state = state_mode
        low, high = (setpoint, self.high) if self.mode == "heat" else (self.low, setpoint)
        s.attributes = {
            "hvac_action": action,
            "target_temp_low": low,
            "target_temp_high": high,
            "current_temperature": indoor_attr,
            "fan_mode": "auto",
        }
        return s


@pytest.mark.parametrize("mode", ["heat", "cool"])
def test_heat_cool_range_thermostat_learns_both_directions(tmp_path, mode):
    rig = _RangeRig(tmp_path, mode)
    sp = 68.0 if mode == "heat" else 74.0
    _settled(rig, sp)
    rig.cycle(_T0, sp=sp, on_indoor=_past(mode, sp, 1.12), off_indoor=_past(mode, sp, -0.2))
    assert rig.count == 1 and rig.raw == pytest.approx(1.12) and rig.swing == 1.5
    other = "cool" if mode == "heat" else "heat"
    assert rig.model()[f"swing_{other}_f"] is None  # independent directions


# ---------------------------------------------------------------------------
# Replay (tools/thermal_replay.py --chart-log shares apply_swing_samples)
# ---------------------------------------------------------------------------


def _replay_rows(mode: str, cycles: list[tuple[float, float, float]]) -> list[dict]:
    """chart_log rows: for each (setpoint, on reading, off reading), a steady poll row then the two edges."""
    running = _MODES[mode]["running"]
    rows: list[dict] = []
    t = _T0
    for sp, on, off in cycles:
        rows.append({"ts": t.isoformat(), "hvac": "idle", "indoor": on, "setpoint": sp})
        rows.append(
            {
                "ts": (t + timedelta(minutes=10)).isoformat(),
                "hvac": running,
                "indoor": on,
                "indoor_primary": on,
                "setpoint": sp,
                "event": "hvac_action_change",
            }
        )
        rows.append(
            {
                "ts": (t + timedelta(minutes=20)).isoformat(),
                "hvac": "idle",
                "indoor": off,
                "indoor_primary": off,
                "setpoint": sp,
                "event": "hvac_action_change",
            }
        )
        t += timedelta(hours=3)
    return rows


@pytest.mark.parametrize("mode", ["heat", "cool"])
def test_replay_gives_each_cycle_the_resolution_it_was_read_at_and_learns_the_same_value(mode):
    """Before #1477 replay inferred ONE resolution for the whole log: a log that switched to the finer sensor
    called every old whole-degree cycle fine, so the fine/coarse rule could not see them."""
    tr = importlib.import_module("tools.thermal_replay")
    sp = 69.0 if mode == "heat" else 75.0
    coarse = [(sp, _past(mode, sp, k), _past(mode, sp, k)) for k in (1.0, 2.0, 1.0, 1.0, 1.0)]
    fine = [(sp, _past(mode, sp, 1.44), _past(mode, sp, 1.08)), (sp, _past(mode, sp, 1.44), _past(mode, sp, 1.02))]
    samples = tr.compute_swing_samples_from_chart_log(_replay_rows(mode, coarse + fine))[mode]
    assert [s["resolution_f"] for s in samples] == [1.0] * 5 + [0.0] * 2
    cache: dict = {}
    tr._swing.apply_swing_samples(cache, mode, samples)
    assert cache[f"swing_{mode}_f"] == 1.5 and cache[f"swing_{mode}_raw_f"] == 1.44


def test_ai_context_shows_the_rounded_swing_its_median_and_celsius_two_decimals():
    from custom_components.climate_advisor.ai_skills_context import _swing_context_lines

    thermal = {
        "swing_heat_f": 1.5,
        "swing_heat_raw_f": 1.44,
        "observation_count_swing_heat": 7,
        "swing_cool_f": None,
        "observation_count_swing_cool": 0,
    }
    f_text = "\n".join(_swing_context_lines(thermal, {"temp_unit": "fahrenheit"}))
    assert "heat swing: 1.5°F below setpoint" in f_text and "median 1.44°F rounded up" in f_text
    c_text = "\n".join(_swing_context_lines(thermal, {"temp_unit": "celsius"}))
    assert "heat swing: 0.83°C below setpoint" in c_text
    assert "cool swing: not measured yet" in c_text


def test_learning_db_summary_line_shows_the_median_behind_the_rounded_value():
    ldb = importlib.import_module("tools.learning_db")
    cache: dict = {}
    swing.apply_swing_samples(cache, "heat", _zone1_window())
    ldb_lines = ldb._format_swing_mode_section(swing, cache, "heat", last_n=0)
    assert any("recomputed 1.50" in line and "median 1.44" in line and "of the 2 finest" in line for line in ldb_lines)
    assert not any("MISMATCH" in line for line in ldb_lines)


# ---------------------------------------------------------------------------
# Owner decision (PR #1482): the bedroom loop on the MEASURED swing, the detectors on the ROUNDED-UP swing
# ---------------------------------------------------------------------------
# Zone 1 as stored by 0.9.17: measured 1.44 F, rounded up 1.5 F. Every pin below sits in the 0.06 F gap between the
# two, so a consumer reading the other value fails.
_ZONE1_MODEL = {"swing_heat_f": 1.5, "swing_heat_raw_f": 1.44, "swing_cool_f": 1.5, "swing_cool_raw_f": 1.26}


@pytest.mark.parametrize(
    ("thermal", "expected"),
    [
        (_ZONE1_MODEL, 1.44),
        ({"swing_heat_f": 0.59}, 0.59),  # no raw (pre-0.9.17 cache or a scenario's model): the value is the measurement
        ({"swing_heat_f": None, "swing_heat_raw_f": None}, THERMAL_SWING_DEFAULT_F),  # no samples: the 1.5 default
        ({}, THERMAL_SWING_DEFAULT_F),
        (None, THERMAL_SWING_DEFAULT_F),
        ({"swing_heat_raw_f": True, "swing_heat_f": 0.0}, THERMAL_SWING_DEFAULT_F),
    ],
)
def test_measured_swing_falls_back_to_the_value_then_the_default(thermal, expected):
    assert swing.measured_swing_f(thermal, "heat") == expected


def test_bedroom_loop_triggers_on_the_measured_swing_not_the_rounded_one():
    """Bedroom 1.48 F under its 66 F target: past the measured 1.44 F, inside the rounded 1.5 F -> the loop raises."""
    from tests.test_remote_offset_1194a import Env

    env = Env(bedroom_f=64.52)
    env.engine.inputs.thermal = dict(_ZONE1_MODEL)
    env.write(66.0)
    env.check()
    assert env.sp_f() == [66.0, 68.0]
    # control: the same bedroom with only the rounded value known holds (1.48 < 1.5)
    env = Env(bedroom_f=64.52)
    env.engine.inputs.thermal = {"swing_heat_f": 1.5}
    env.write(66.0)
    env.check()
    assert env.sp_f() == [66.0]


def test_bedroom_loop_with_nothing_learned_uses_the_default():
    from tests.test_remote_offset_1194a import Env

    env = Env(bedroom_f=64.6)  # 1.4 under the target: inside the 1.5 default
    env.engine.inputs.thermal = {"swing_heat_f": None, "swing_heat_raw_f": None}
    env.write(66.0)
    env.check()
    assert env.sp_f() == [66.0]


def _detector_coord(mode: str = "heat"):
    from tests.test_comfort_swing_tolerance_1258 import _coord

    coord = _coord(mode=mode)
    coord.learning.get_thermal_model.return_value = dict(_ZONE1_MODEL)
    return coord


def test_incident_scan_uses_the_rounded_swing():
    """1.47 F under the 68 F floor in heat: inside the rounded 1.5 F (no incident), past the measured 1.44 F."""
    from tests.test_comfort_swing_tolerance_1258 import _scan

    assert _scan(_detector_coord(), 66.53) == []
    assert [e["incident_class"] for e in _scan(_detector_coord(), 66.45)] == ["comfort_undertemp"]


def test_violation_minutes_and_stall_alert_tolerance_use_the_rounded_swing():
    coord = _detector_coord()
    assert coord._is_tolerated_comfort_deviation(66.53, 68.0, 74.0) is True  # violation minutes + #1050 stall alert
    assert coord._is_within_thermostat_hysteresis(66.53, 68.0, 74.0) is True
    cool = _detector_coord("cool")
    assert cool._is_tolerated_comfort_deviation(75.47, 68.0, 74.0) is True  # cool mirror: measured 1.26 < 1.47


def test_incident_payload_records_the_rounded_tolerance():
    ctx = _detector_coord()._hysteresis_incident_context("heat")
    assert ctx["swing_tolerance_f"] == 1.5 and ctx["swing_tolerance_source"] == "learned"


def test_direction_rule_uses_the_rounded_swing():
    """Thermostat model: within one swing above the floor, cooler air would only start the heat (vetoed)."""
    import types

    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    coord = _detector_coord()
    coord._outdoor_direction_ok = types.MethodType(ClimateAdvisorCoordinator._outdoor_direction_ok, coord)
    coord.automation_engine._paused_by_door = False
    assert coord._outdoor_direction_ok(60.0, 69.47, 68.0, 74.0) is False  # 1.47 above the floor: inside 1.5


def test_ai_cross_validation_uses_the_rounded_swing():
    import asyncio

    from custom_components.climate_advisor.ai_skills_context import build_state_cross_validation_context
    from tests.test_ai_skills_context_state_cross_validation import _make_coordinator, _make_hass

    learning = MagicMock()
    learning.get_thermal_model.return_value = dict(_ZONE1_MODEL)
    coord = _make_coordinator(data={"hvac_mode": "heat", "hvac_action": "idle"}, learning=learning)
    ctx = asyncio.run(build_state_cross_validation_context(_make_hass(current_temperature=66.53), coord))
    assert "[FLAG]" not in ctx and "within tolerance" in ctx and "1.5" in ctx


def test_night_strategy_emulates_the_bedroom_loop_on_the_measured_swing():
    """The emulated #1194 loop follows ``loop_swing_*`` (the measured swing); the score keeps the rounded one."""
    import dataclasses

    from custom_components.climate_advisor import night_strategy as ns
    from tests.test_night_strategy import BED, HALL, cold_night

    cold_bed = cold_night(readings={HALL: 68.0, BED: 64.4})
    split = ns.plan_night(dataclasses.replace(cold_bed, swing_heat_f=1.5, loop_swing_heat_f=0.3))
    loop_only = ns.plan_night(dataclasses.replace(cold_bed, swing_heat_f=0.3))
    rounded = ns.plan_night(dataclasses.replace(cold_bed, swing_heat_f=1.5))
    a, b, c = (p.option(ns.OPT_CLOSED) for p in (split, loop_only, rounded))
    assert a.heat_min == b.heat_min and a.bed_wake_f == b.bed_wake_f  # same emulated loop
    assert a.heat_min != c.heat_min  # ... which is not the rounded value's loop


def test_chart_payload_and_dashboards_carry_both_values():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "custom_components" / "climate_advisor"
    coord_src = (src / "coordinator.py").read_text()
    assert '"swing_heat_raw": display_delta(measured_swing_f(thermal_model, "heat"), unit, 2)' in coord_src
    assert '"swing_cool_raw": display_delta(measured_swing_f(thermal_model, "cool"), unit, 2)' in coord_src
    assert display_delta(swing.measured_swing_f(_ZONE1_MODEL, "heat"), "celsius", 2) == 0.8  # 1.44 F x 5/9
