"""Issue #1512 / #1516: the pooled cycle model's wiring (recorder -> store -> fit -> learning -> surfaces).

Occupant outcome: every heating or cooling run the thermostat makes (short pulses, long recoveries, runs followed by a
coast, runs cut by a restart or an unavailable thermostat) becomes evidence about how fast THIS house heats or cools;
the rate the house is judged by sharpens with each cycle, is never worse than the typical-house default, and an
unavailable thermostat is never mistaken for the equipment stopping.

Real coordinator methods bound onto a bare coordinator, the real ``CycleRecorder`` / ``CycleStore`` /
``cycle_model`` and a real ``LearningEngine`` on a tmp path (the HA side is stubbed).
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor import coordinator as _coord_mod  # noqa: E402
from custom_components.climate_advisor.const import (  # noqa: E402
    CYCLE_FLAG_WINDOWS,
    OBS_TYPE_HVAC_COOL,
    OBS_TYPE_HVAC_HEAT,
)
from custom_components.climate_advisor.cycle_recorder import CycleRecorder  # noqa: E402
from custom_components.climate_advisor.cycle_store import CycleStore  # noqa: E402
from custom_components.climate_advisor.learning import (  # noqa: E402
    CYCLE_FIT_KEYS,
    LearningEngine,
    aged_rates,
)
from custom_components.climate_advisor.thermal_confidence import grade_hvac_confidence  # noqa: E402

Coord = _coord_mod.ClimateAdvisorCoordinator
_COORD_MOD = "custom_components.climate_advisor.coordinator"
_T0 = datetime(2026, 11, 1, 1, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------------------------------------------
# learning.apply_cycle_fit and the grade
# ---------------------------------------------------------------------------------------------------------------


def _fit(q=6.0, grade="medium", ne=5, **kw):
    out = {
        "q": q,
        "sd_q": 0.8,
        "k_p_cycle": -0.03,
        "sd_k": 0.01,
        "coast_area_f": 0.4,
        "tau": 4.0,
        "ne": ne,
        "n_cycles": ne + 2,
        "er_q": 0.8,
        "grade": grade,
        "resolution_f": 0.1,
    }
    out.update(kw)
    return out


def _engine(tmp_path) -> LearningEngine:
    e = LearningEngine(tmp_path)
    e.load_state()
    e._state.thermal_model_cache = {
        "k_passive": -0.05,
        "k_active_heat": 3.0,
        "k_active_cool": -3.5,
        "observation_count_heat": 40,
        "observation_count_cool": 40,
    }
    return e


class TestApplyCycleFit:
    @pytest.mark.parametrize(("mode", "q", "field"), [("heat", 6.2, "k_active_heat"), ("cool", -7.1, "k_active_cool")])
    def test_v3_primary_writes_q_and_the_grade_becomes_the_confidence(self, tmp_path, mode, q, field):
        """Heat and cool mirror: q lands in k_active_*, the derived display rate follows, k_passive is untouched."""
        e = _engine(tmp_path)
        assert e.apply_cycle_fit(mode, _fit(q=q), primary_is_v3=True) is True
        c = e._state.thermal_model_cache
        assert c[field] == pytest.approx(q)
        assert c["k_passive"] == -0.05  # the closed-house leak rate is never the cycle model's
        assert c[f"cycle_fit_{mode}"]["k_p_cycle"] == -0.03 and c[f"cycle_fit_{mode}"]["applied"] is True
        assert c[f"observation_count_{mode}"] == 5  # eligible cycles counted
        model = e.get_thermal_model()
        assert model["confidence"] == "medium"
        assert model["heating_rate_f_per_hour" if mode == "heat" else "cooling_rate_f_per_hour"] == round(abs(q), 2)
        assert set(CYCLE_FIT_KEYS) <= set(c[f"cycle_fit_{mode}"])

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_v4_primary_stores_the_fit_but_keeps_the_aged_rates(self, tmp_path, mode):
        e = _engine(tmp_path)
        before = dict(e._state.thermal_model_cache)
        assert e.apply_cycle_fit(mode, _fit(q=9.9 if mode == "heat" else -9.9), primary_is_v3=False) is False
        c = e._state.thermal_model_cache
        assert c["k_active_heat"] == before["k_active_heat"] and c["k_active_cool"] == before["k_active_cool"]
        assert c[f"cycle_fit_{mode}"]["applied"] is False and c[f"cycle_fit_{mode}"]["q"] is not None

    def test_a_fit_with_no_eligible_cycle_never_overwrites_the_rates(self, tmp_path):
        """Grade none (ne 0, the prior returned unchanged): stored for display, not applied even on v3."""
        e = _engine(tmp_path)
        assert e.apply_cycle_fit("heat", _fit(q=8.0, grade="none", ne=0), primary_is_v3=True) is False
        assert e._state.thermal_model_cache["k_active_heat"] == 3.0
        assert grade_hvac_confidence(e._state.thermal_model_cache) != "none"  # legacy count grade still reads

    def test_ageing_does_not_pull_an_applied_rate_back_to_the_retired_observations(self, tmp_path):
        e = _engine(tmp_path)
        e._state.thermal_observations = [
            {
                "hvac_mode": "heat",
                "k_active": 2.0,
                "date": datetime.now(UTC).date().isoformat(),
                "confidence_grade": "high",
                "method": "run_edge",
            }
        ]
        e.apply_cycle_fit("heat", _fit(q=6.5), primary_is_v3=True)
        e._recompute_aged_rates()
        assert e._state.thermal_model_cache["k_active_heat"] == pytest.approx(6.5)
        # the stored old observation still ages (it is read by aged_rates), it just does not override an applied q
        assert aged_rates(e._state.thermal_observations, datetime.now(UTC).date())["k_active_heat"] == 2.0

    @pytest.mark.parametrize(("grade", "expected"), [("low", "low"), ("medium", "medium"), ("high", "high")])
    def test_grade_mapping_takes_the_better_mode(self, grade, expected):
        cache = {
            "cycle_fit_heat": {"applied": True, "grade": grade},
            "cycle_fit_cool": {"applied": True, "grade": "none"},
            "observation_count_heat": 0,
            "observation_count_cool": 0,
        }
        assert grade_hvac_confidence(cache) == expected

    def test_unapplied_fit_does_not_change_the_legacy_grade(self):
        cache = {"cycle_fit_heat": {"applied": False, "grade": "high"}, "observation_count_heat": 0}
        assert grade_hvac_confidence(cache) == "none"

    def test_retired_writers_are_gone(self, tmp_path):
        e = _engine(tmp_path)
        assert not hasattr(e, "record_hvac_run") and not hasattr(e, "record_duty_balance_fit")
        from custom_components.climate_advisor import learning

        assert not hasattr(learning, "compute_hvac_run_rate")
        assert "duty_balance" in learning._CHART_LOG_FIT_SOURCES  # stored fits keep deduping and ageing


# ---------------------------------------------------------------------------------------------------------------
# coordinator wiring
# ---------------------------------------------------------------------------------------------------------------


def _state(mode="heat", action="heating", setpoint=70.0, temp=66.0):
    return SimpleNamespace(
        state=mode,
        attributes={"hvac_action": action, "temperature": setpoint, "current_temperature": temp},
    )


class _Rig:
    """A bare coordinator with the real cycle methods, a virtual clock and a task collector."""

    def __init__(self, tmp_path, *, v3_primary=True, config=None):
        self.clock = _T0
        self.indoor: float | None = 66.0
        self.tasks: list = []
        c = object.__new__(Coord)
        self.c = c
        c.config = {"learning_enabled": True, "temp_unit": "fahrenheit", **(config or {})}
        c.learning = LearningEngine(tmp_path)
        c.learning.load_state()
        c.learning._state.thermal_model_cache = {"k_passive": -0.05}
        c._cycle_recorder = CycleRecorder(True)
        c._cycle_store = CycleStore(tmp_path / "cycles.json")
        c._cycle_fit_lock = None
        c._rejection_log = {}
        c._today_record = SimpleNamespace(thermal_session_count=0)
        c._last_outdoor_temp = 40.0
        c.automation_engine = SimpleNamespace(_fan_active=False, _fan_override_active=False, _natural_vent_active=False)
        c._any_sensor_open = lambda: self.windows
        self.windows = False
        c._get_indoor_temp_with_provenance = lambda: SimpleNamespace(primary_value=self.indoor)
        c._primary_indoor_entity = lambda: "sensor.hall"
        c._read_thermostat_setpoint_f = lambda state, mode: state.attributes.get("temperature")
        c._zone_log_id = lambda: "z1"
        c._spawn = lambda coro: self.tasks.append(coro)

        async def _job(fn, *args):
            return fn(*args)

        c._executor_job = _job
        c.control_thermal_model = (lambda base: base) if v3_primary else (lambda base: dict(base or {}))
        for name in (
            "_cycles_enabled",
            "_cycle_primary_is_v3",
            "_cycle_flag_state",
            "_cycle_collect",
            "_cycle_flags",
            "_cycle_outdoor",
            "_cycle_reading",
            "_cycle_tick",
            "_cycle_gap",
            "_cycle_start_midrun",
            "_cycle_thermostat_event",
            "_cycle_lock",
            "_cycle_process",
            "_cycle_resolution",
            "_cycle_refit",
            "_cycle_record_rejection",
            "_persist_cycles",
            "_append_rejection",
            "_build_learning_health",
            "_cycle_model_rows",
        ):
            setattr(c, name, types.MethodType(getattr(Coord, name), c))
        c._cycle_dir = Coord._cycle_dir
        dt = MagicMock()
        dt.now.side_effect = lambda: self.clock
        self.patch = patch(f"{_COORD_MOD}.dt_util", dt)
        self.patch.start()

    def close(self):
        self.patch.stop()

    def at(self, minutes: float):
        self.clock = _T0 + timedelta(seconds=minutes * 60)

    def drain(self):
        async def _run():
            while self.tasks:
                await self.tasks.pop(0)

        asyncio.run(_run())

    def thermostat(self, old, new, was, now):
        self.c._cycle_thermostat_event(old, new, was, now)


@pytest.fixture
def rig(tmp_path):
    r = _Rig(tmp_path)
    yield r
    r.close()


def _heat_cycle(r: _Rig, start_min=0.0, run_min=30.0, rate=6.0, coast=True, res=0.1, setpoint_change_at=None):
    """Feed one clean heating run (reading every 30 s) and its coast through the real hooks."""
    on, off = _state("heat", "idle"), _state("heat", "heating")
    t = 60.0
    r.at(start_min)
    r.indoor = t
    r.thermostat(on, off, False, True)
    steps = int(run_min * 2)
    for i in range(1, steps + 1):
        r.at(start_min + i * 0.5)
        r.indoor = round(round((t + rate * i * 0.5 / 60.0) / res) * res, 2)
        if setpoint_change_at is not None and abs(i * 0.5 - setpoint_change_at) < 1e-9:
            r.thermostat(_state("heat", "heating", setpoint=70.0), _state("heat", "heating", setpoint=72.0), True, True)
        r._rig_last = r.indoor
        r.c._cycle_reading()
    r.at(start_min + run_min)
    r.thermostat(_state("heat", "heating"), _state("heat", "idle"), True, False)
    if coast:
        top = r.indoor
        for i in range(1, 31):
            r.at(start_min + run_min + i * 0.5)
            r.indoor = round(top + 0.4 * (1 - 0.5 ** (i / 6.0)), 2)
            r.c._cycle_reading()
    r.at(start_min + run_min + 46)
    r.c._cycle_tick()
    r.drain()


class TestRecorderFeed:
    def test_heating_cycle_is_recorded_stored_fitted_and_applied(self, rig):
        _heat_cycle(rig)
        (cyc,) = rig.c._cycle_store.cycles("heat")
        assert cyc["cut"] is None and cyc["t_off"] is not None and len(cyc["samples"]) > 40
        c = rig.c.learning._state.thermal_model_cache
        fit = c["cycle_fit_heat"]
        assert fit["ne"] == 1 and fit["grade"] != "none"
        assert fit["q"] == pytest.approx(6.0, abs=2.5)
        assert fit["applied"] is True and c["k_active_heat"] == pytest.approx(fit["q"], abs=1e-3)
        assert c["k_passive"] == -0.05
        assert rig.c._today_record.thermal_session_count == 1  # an eligible cycle counts as a session
        assert rig.c._rejection_log == {}

    def test_cool_cycle_mirror(self, rig):
        on, run = _state("cool", "idle"), _state("cool", "cooling")
        rig.at(0)
        rig.indoor = 78.0
        rig.thermostat(on, run, False, True)
        for i in range(1, 61):
            rig.at(i * 0.5)
            rig.indoor = round(78.0 - 6.0 * i * 0.5 / 60.0, 1)
            rig.c._cycle_reading()
        rig.at(30)
        rig.thermostat(run, on, True, False)
        rig.at(30 + 46)
        rig.c._cycle_tick()
        rig.drain()
        (cyc,) = rig.c._cycle_store.cycles("cool")
        assert cyc["mode"] == "cool"
        fit = rig.c.learning._state.thermal_model_cache["cycle_fit_cool"]
        assert fit["q"] < 0 and fit["ne"] == 1
        assert rig.c.learning._state.thermal_model_cache["k_active_cool"] == pytest.approx(fit["q"], abs=1e-3)

    def test_unavailable_thermostat_is_a_gap_not_an_off_edge(self, rig):
        """heating -> unavailable closes the cycle with cut='unavailable' and no off edge; the coast is not learned."""
        rig.at(0)
        rig.thermostat(_state("heat", "idle"), _state("heat", "heating"), False, True)
        for i in range(1, 11):
            rig.at(i * 0.5)
            rig.indoor = 60.0 + i * 0.1
            rig.c._cycle_reading()
        rig.at(6)
        # the runtime layer sees unavailable as "not running": the cycle feed must still call it a gap
        rig.thermostat(_state("heat", "heating"), SimpleNamespace(state="unavailable", attributes={}), True, False)
        rig.drain()
        (cyc,) = rig.c._cycle_store.cycles("heat")
        assert cyc["cut"] == "unavailable" and cyc["t_off"] is None
        # its clean pre-gap part is still evidence: the model counts it, so no rejection is recorded for it
        assert rig.c.learning._state.thermal_model_cache["cycle_fit_heat"]["ne"] == 1
        # unknown after unavailable is the same gap: nothing more happens
        rig.thermostat(
            SimpleNamespace(state="unavailable", attributes={}),
            SimpleNamespace(state="unknown", attributes={}),
            False,
            False,
        )
        assert rig.tasks == []

    def test_restart_gap_at_startup_flags_the_first_reading_and_midrun_start_is_recorded(self, rig):
        rig.c._cycle_gap("restart")  # nothing open: only the gap flag is armed
        rig.at(0)
        rig.c._cycle_start_midrun("heat", _state("heat", "heating"))
        rig.at(0.5)
        rig.indoor = 66.2
        rig.c._cycle_reading()
        rig.at(3)
        rig.c._cycle_gap("restart")  # HA stop closes it with its clean part
        rig.drain()
        (cyc,) = rig.c._cycle_store.cycles("heat")
        assert cyc["cut"] == "restart"
        assert cyc["samples"][0][2] & 16  # gap_before on the first post-restart sample
        # three minutes with one reading after the gap: the model's own verdict names why it is not usable evidence
        assert rig.c._rejection_log[OBS_TYPE_HVAC_HEAT][0]["reason_code"] in ("cycle_gap_restart", "cycle_too_short")

    def test_setpoint_change_closes_only_a_run_that_continues_through_it(self, rig):
        _heat_cycle(rig, setpoint_change_at=10.0, coast=False)
        (cyc,) = rig.c._cycle_store.cycles("heat")
        assert cyc["cut"] == "setpoint_change"
        assert rig.c._rejection_log[OBS_TYPE_HVAC_HEAT][-1]["reason_code"] == "cycle_setpoint_change"

    def test_a_run_that_starts_with_the_setpoint_change_is_a_valid_cycle(self, rig):
        """Raising the setpoint starts the furnace in the same event: that is the cycle, not an excluded one."""
        rig.at(0)
        rig.thermostat(_state("heat", "idle", setpoint=68.0), _state("heat", "heating", setpoint=72.0), False, True)
        assert rig.c._cycle_recorder.is_running("heat")

    def test_window_open_during_the_run_is_labelled_excluded(self, rig):
        rig.windows = True
        _heat_cycle(rig, coast=False)
        codes = [e["reason_code"] for e in rig.c._rejection_log.get(OBS_TYPE_HVAC_HEAT, [])]
        assert codes == ["cycle_excluded_window_fan"]
        (cyc,) = rig.c._cycle_store.cycles("heat")
        assert any(s[2] & CYCLE_FLAG_WINDOWS for s in cyc["samples"])
        assert rig.c._today_record.thermal_session_count == 0

    def test_short_whole_degree_run_is_rejected_with_its_values(self, rig):
        """A 2 minute pulse on a whole-degree sensor cannot move 3 steps: a rejection with the model's code."""
        _heat_cycle(rig, run_min=2.0, rate=6.0, res=1.0, coast=False)
        (ev,) = rig.c._rejection_log[OBS_TYPE_HVAC_HEAT]
        assert ev["reason_code"] in ("cycle_too_short", "cycle_signal_below_resolution")
        assert ev["minutes"] == pytest.approx(2.0, abs=0.1) and "steps" in ev and ev["cycle_id"]
        # the fit is still stored: never worse than the prior (grade none = prior unchanged)
        assert rig.c.learning._state.thermal_model_cache["cycle_fit_heat"]["grade"] == "none"
        assert rig.c.learning._state.thermal_model_cache.get("k_active_heat") is None

    def test_learning_disabled_records_nothing(self, tmp_path):
        r = _Rig(tmp_path, config={"learning_enabled": False})
        try:
            r.thermostat(_state("heat", "idle"), _state("heat", "heating"), False, True)
            assert not r.c._cycle_recorder.is_running()
        finally:
            r.close()

    def test_v4_primary_shows_the_fit_but_leaves_decisions_rates(self, tmp_path):
        r = _Rig(tmp_path, v3_primary=False)
        try:
            _heat_cycle(r)
            c = r.c.learning._state.thermal_model_cache
            assert c["cycle_fit_heat"]["applied"] is False and c["cycle_fit_heat"]["ne"] == 1
            assert c.get("k_active_heat") is None  # untouched
            (row,) = [x for x in r.c._cycle_model_rows("fahrenheit") if x["mode"] == "heat"]
            assert (
                row["source"] == "cycle"
                and row["applied"] is False
                and row["q"] == pytest.approx(c["cycle_fit_heat"]["q"], abs=1e-3)
            )
        finally:
            r.close()

    def test_store_survives_a_reload_and_the_refit_reads_it(self, rig, tmp_path):
        _heat_cycle(rig)
        again = CycleStore(tmp_path / "cycles.json")
        again.load()
        assert len(again.cycles("heat")) == 1

    def test_ha_stop_keeps_the_pre_restart_part_of_an_open_cycle(self, rig):
        rig.at(0)
        rig.thermostat(_state("heat", "idle"), _state("heat", "heating"), False, True)
        for i in range(1, 8):
            rig.at(i * 0.5)
            rig.indoor = 60.0 + i * 0.1
            rig.c._cycle_reading()
        asyncio.run(rig.c._persist_cycles())
        (cyc,) = rig.c._cycle_store.cycles("heat")
        assert cyc["cut"] == "restart" and cyc["t_off"] is None


class TestSurfaces:
    def test_learning_health_lists_cycle_codes_and_the_fit(self, rig):
        _heat_cycle(rig)
        health = rig.c._build_learning_health()
        counts = health[OBS_TYPE_HVAC_HEAT]["rejections"]
        for code in (
            "cycle_too_short",
            "cycle_signal_below_resolution",
            "cycle_excluded_window_fan",
            "cycle_setpoint_change",
            "cycle_gap_restart",
            "cycle_thermostat_unavailable",
        ):
            assert code in counts
        assert health["cycle_fit_heat"]["grade"] != "none" and health["cycle_fit_cool"] is None
        assert OBS_TYPE_HVAC_COOL in health

    def test_decision_rows_carry_the_model_in_display_units(self, tmp_path):
        r = _Rig(tmp_path)
        try:
            _heat_cycle(r)
            fit = r.c.learning._state.thermal_model_cache["cycle_fit_heat"]
            (f,) = [x for x in r.c._cycle_model_rows("fahrenheit") if x["mode"] == "heat"]
            (cel,) = [x for x in r.c._cycle_model_rows("celsius") if x["mode"] == "heat"]
            assert f["source"] == "cycle" and f["q"] == pytest.approx(fit["q"], abs=1e-3)
            assert cel["q"] == pytest.approx(fit["q"] * 5 / 9, abs=2e-3)  # rates follow the display unit
            assert f["coast_area_f"] == fit["coast_area_f"] == cel["coast_area_f"]  # degF delta, the UI converts it
            for key in ("sd_q", "grade", "k_p_cycle", "ne", "n_cycles", "resolution_f"):
                assert key in f
        finally:
            r.close()


class TestStructure:
    def test_primary_is_read_through_the_control_feed_not_the_engine_setting(self):
        """R6b: _cycle_primary_is_v3 names no engine selection; v3 primary is control_thermal_model's identity."""
        import inspect

        src = inspect.getsource(Coord._cycle_primary_is_v3)
        for banned in (
            "primary_engine_id",
            "primary_id",
            "CONF_PRIMARY_ENGINE",
            "prediction_engines",
            "engine_running",
        ):
            assert banned not in src.replace('"""', "").split("probe")[1]

    def test_retired_coordinator_symbols_are_gone(self):
        for name in (
            "_closed_house_rate",
            "_reject_hvac_run",
            "_run_duty_balance_chart_log_fit",
            "_maybe_run_periodic_duty_balance_fit",
        ):
            assert not hasattr(Coord, name), name
        assert not hasattr(_coord_mod, "hvac_rate_fit")
