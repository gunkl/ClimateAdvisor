"""Issue #1512 reopened (owner 2026-11-12): blower around the cycle, sensor lag, honest grades, information weights.

What the occupant gets:

* A thermostat that runs the blower after the burner or compressor stops (Ecobee: ``heating -> idle`` and, 1 s later,
  ``fan`` for 2 minutes; other thermostats ``heating -> fan -> idle``), or before it starts (``idle -> fan ->
  heating``), no longer has that blower period read as a 2-minute heating or cooling run. The real cycle keeps its
  coast (the room kept rising 67.1 -> 68.4 F after the stop on 2026-11-12), so pre-heat can allow for it.
* A fine sensor that lags the air by a couple of minutes no longer makes a big system look 22-37 % weaker.
* The grade says honestly what the data can tell (a typical-house leak rate, an unknown lag on short runs).
* A long accurate run is not pushed aside by a run of short pulses.
* A whole- or half-degree sensor is named, with what Climate Advisor uses instead.
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor import cycle_model as cm  # noqa: E402
from custom_components.climate_advisor import hvac_action as ha  # noqa: E402
from custom_components.climate_advisor.learning import LearningEngine  # noqa: E402
from tests.test_cycle_model_1512 import fit, synth  # noqa: E402
from tests.test_cycle_wiring_1512 import _Rig, _state  # noqa: E402
from tests.test_hvac_session_detection import (  # noqa: E402
    _make_state,
    _make_thermostat_coord,
    _make_thermostat_event,
)

_DT = "custom_components.climate_advisor.coordinator.dt_util"
_ACTION = {"heat": "heating", "cool": "cooling"}


def _fire(coord, when: datetime, old: tuple[str, str], new: tuple[str, str]) -> None:
    event = _make_thermostat_event(_make_state(old[0], old[1]), _make_state(new[0], new[1]))
    with patch(_DT) as mock_dt:
        mock_dt.now.return_value = when
        asyncio.run(coord._async_thermostat_changed(event))


def _t(minute: float) -> datetime:
    return datetime(2026, 11, 12, 6, 0, 0) + __import__("datetime").timedelta(seconds=minute * 60)


# ---------------------------------------------------------------------------------------------------------------
# 1. the blower around the cycle: sessions, runtime, the legacy observation
# ---------------------------------------------------------------------------------------------------------------


class TestBlowerIsNeverARun:
    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_ecobee_overrun_idle_then_fan_is_not_a_second_run(self, mode):
        """heating -> idle, fan 1 s later for 2 min -> idle: one run of 8 min, one observation, no phantom."""
        coord = _make_thermostat_coord()
        act = _ACTION[mode]
        _fire(coord, _t(0), (mode, "idle"), (mode, act))
        _fire(coord, _t(8), (mode, act), (mode, "idle"))
        _fire(coord, _t(8 + 1 / 60), (mode, "idle"), (mode, "fan"))
        assert coord._hvac_on_since is None, "the blower overrun must not arm the runtime timer"
        _fire(coord, _t(10), (mode, "fan"), (mode, "idle"))
        assert coord._today_record.hvac_runtime_minutes == pytest.approx(8.0)
        coord._start_hvac_observation.assert_called_once_with(mode)
        assert coord._thermostat_reports_action is True

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_overrun_straight_from_the_run(self, mode):
        """The simulator's sequence: heating -> fan -> idle."""
        coord = _make_thermostat_coord()
        act = _ACTION[mode]
        _fire(coord, _t(0), (mode, "idle"), (mode, act))
        _fire(coord, _t(8), (mode, act), (mode, "fan"))
        _fire(coord, _t(10), (mode, "fan"), (mode, "idle"))
        assert coord._today_record.hvac_runtime_minutes == pytest.approx(8.0)
        coord._start_hvac_observation.assert_called_once_with(mode)

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_lead_in_before_the_start_is_not_part_of_the_run(self, mode):
        """idle -> fan (lead-in) -> heating: runtime and the observation start at the burner, not at the blower."""
        coord = _make_thermostat_coord()
        act = _ACTION[mode]
        _fire(coord, _t(0), (mode, "idle"), (mode, act))  # an earlier run: the thermostat reports its burner
        _fire(coord, _t(5), (mode, act), (mode, "idle"))
        _fire(coord, _t(60), (mode, "idle"), (mode, "fan"))  # lead-in
        assert coord._hvac_on_since is None
        _fire(coord, _t(61), (mode, "fan"), (mode, act))
        _fire(coord, _t(71), (mode, act), (mode, "fan"))
        _fire(coord, _t(73), (mode, "fan"), (mode, "idle"))
        assert coord._today_record.hvac_runtime_minutes == pytest.approx(15.0)
        assert coord._start_hvac_observation.call_count == 2

    def test_mode_off_mid_run_then_overrun(self):
        """Switched Off mid-run, the unit goes straight into overrun (fan whatever the mode): no new session."""
        coord = _make_thermostat_coord()
        _fire(coord, _t(0), ("heat", "idle"), ("heat", "heating"))
        _fire(coord, _t(6), ("heat", "heating"), ("off", "fan"))
        _fire(coord, _t(8), ("off", "fan"), ("off", "idle"))
        assert coord._today_record.hvac_runtime_minutes == pytest.approx(6.0)
        coord._start_hvac_observation.assert_called_once_with("heat")

    @pytest.mark.parametrize("action", ["", "fan"])
    def test_thermostat_never_seen_heating_keeps_the_93_fallback(self, action):
        """A thermostat that only ever reports fan (or nothing) while it heats still gets its sessions (#93)."""
        coord = _make_thermostat_coord()
        _fire(coord, _t(0), ("off", action), ("heat", action))
        _fire(coord, _t(50), ("heat", action), ("off", action))
        assert coord._today_record.hvac_runtime_minutes == pytest.approx(50.0)
        assert getattr(coord, "_thermostat_reports_action", False) is False

    def test_fan_only_mode_is_still_a_fan_only_session(self):
        """Learned blower rule never touches the fan_only session (#912)."""
        coord = _make_thermostat_coord()
        _fire(coord, _t(0), ("heat", "idle"), ("heat", "heating"))
        _fire(coord, _t(5), ("heat", "heating"), ("heat", "idle"))
        _fire(coord, _t(10), ("heat", "idle"), ("fan_only", "fan"))
        _fire(coord, _t(40), ("fan_only", "fan"), ("off", "idle"))
        assert coord._today_record.thermostat_fan_only_runtime_minutes == pytest.approx(30.0)
        assert coord._today_record.hvac_runtime_minutes == pytest.approx(5.0)

    def test_session_still_running_rule_matches(self):
        """The rollover's stale-session rule (#1241) reads the same blower rule."""
        assert ha.thermostat_reports_running("heat", "fan", ca_ventilating=False) is True  # #93
        assert ha.thermostat_reports_running("heat", "fan", ca_ventilating=False, fan_is_blower=True) is False
        assert ha.thermostat_reports_running("fan_only", "fan", ca_ventilating=False, fan_is_blower=True) is True
        assert ha.thermostat_reports_running("cool", "cooling", ca_ventilating=False, fan_is_blower=True) is True

    def test_overrun_after_idle_counts_as_a_post_run_blower_for_the_fan_reconcile(self):
        """Mirror of #618: idle -> fan within the overrun window is the post-run blower, so the fan reconcile never
        force-writes HVAC off because of it (it used to be judged only on heating/cooling -> fan)."""
        coord = _make_thermostat_coord()
        coord._derive_thermostat_fan_running_for_reconcile = MagicMock(return_value=True)
        coord._get_indoor_temp = MagicMock(return_value=67.0)
        _fire(coord, _t(0), ("heat", "idle"), ("heat", "heating"))
        _fire(coord, _t(8), ("heat", "heating"), ("heat", "idle"))
        _fire(coord, _t(8 + 1 / 60), ("heat", "idle"), ("heat", "fan"))
        rec = coord.automation_engine.reconcile_fan_on_startup
        assert rec.await_count >= 1
        assert rec.await_args.kwargs["recent_hvac_session_ended"] is True
        coord.automation_engine.note_observed_fan_origin.assert_any_call("post_heat_tail")


# ---------------------------------------------------------------------------------------------------------------
# 2. the cycle: one cycle, the coast tracked through the blower period
# ---------------------------------------------------------------------------------------------------------------


@pytest.fixture
def edge_rig(tmp_path):
    """The cycle rig with the REAL edge rule (``_track_hvac_runtime_edges``) in front of the recorder."""
    r = _Rig(tmp_path)
    c = r.c
    Coord = type(c)
    for name in (
        "_track_hvac_runtime_edges",
        "_note_thermostat_action",
        "_fan_is_blower_only",
        "_blower_overrun_recent",
    ):
        if hasattr(Coord, name):  # tolerant: the same test runs against an older tree to show it fails there
            setattr(c, name, types.MethodType(getattr(Coord, name), c))
    c._hvac_session_mode = None
    c._hvac_on_since = None
    c._thermostat_fan_only_on_since = None
    c._start_runtime_timer = MagicMock()
    c._flush_runtime_for_mode = MagicMock()
    c._start_hvac_observation = AsyncMock()
    c._end_hvac_active_phase = MagicMock()
    c._glide_hvac_session_mode = MagicMock()
    c._abandon_observation = MagicMock()
    c._async_save_state = AsyncMock()
    yield r
    r.close()


def _edge(r, old, new):
    asyncio.run(r.c._track_hvac_runtime_edges(old, new))


def _coasting_cycles(r, mode: str, *, sequence: str, n: int = 6) -> None:
    """n runs of 8 min at 9 F/h, then a coast that keeps going 0.6 F over 10 min, the blower on for 2 minutes."""
    act = _ACTION[mode]
    sgn = 1.0 if mode == "heat" else -1.0
    t0 = 0.0
    level = 67.0 if mode == "heat" else 76.0
    for _ in range(n):
        r.at(t0)
        if sequence == "lead_in":
            _edge(r, _state(mode, "idle"), _state(mode, "fan"))
            r.at(t0 + 1)
            _edge(r, _state(mode, "fan"), _state(mode, act))
            on = t0 + 1
        else:
            _edge(r, _state(mode, "idle"), _state(mode, act))
            on = t0
        T = level
        for i in range(1, 17):
            r.at(on + i * 0.5)
            r.indoor = round(T + sgn * 9.0 * i * 0.5 / 60.0, 2)
            r.c._cycle_reading()
        top = r.indoor
        r.at(on + 8)
        if sequence == "fan_direct":
            _edge(r, _state(mode, act), _state(mode, "fan"))
        else:
            _edge(r, _state(mode, act), _state(mode, "idle"))
            r.at(on + 8 + 1 / 60)
            _edge(r, _state(mode, "idle"), _state(mode, "fan"))
        for i in range(1, 61):
            r.at(on + 8 + i * 0.5)
            if abs(i * 0.5 - 2.0) < 1e-9:
                _edge(r, _state(mode, "fan"), _state(mode, "idle"))
            coast = 0.6 * (1 - 0.5 ** (i * 0.5 / 3.0))
            leak = 0.02 * i * 0.5 / 60.0 * 20.0
            r.indoor = round(top + sgn * (coast - leak), 2)
            r.c._cycle_reading()
        t0 = on + 8 + 46
        r.at(t0 - 0.5)
        r.c._cycle_tick()
        r.drain()
    r.at(t0 + 1)
    r.c._cycle_tick()
    r.drain()


class TestCoastThroughTheBlower:
    @pytest.mark.parametrize("mode", ["heat", "cool"])
    @pytest.mark.parametrize("sequence", ["idle_then_fan", "fan_direct", "lead_in"])
    def test_one_cycle_per_run_and_the_coast_is_recorded_and_learned(self, edge_rig, mode, sequence):
        _coasting_cycles(edge_rig, mode, sequence=sequence)
        cycles = edge_rig.c._cycle_store.cycles(mode)
        assert len(cycles) == 6, "a blower period must never open a cycle of its own"
        for c in cycles:
            assert c["t_off"] - c["t_on"] == pytest.approx(8 * 60, abs=1.0), "the run is the burner/compressor only"
            after = [s for s in c["samples"] if s[0] > c["t_off"] + 1]
            assert len(after) >= 20, "the coast through and after the blower belongs to the cycle"
            assert after[-1][0] - c["t_off"] > 25 * 60
        assert not any(
            r.get("reason_code") == "cycle_too_short"
            for rows in (edge_rig.c._rejection_log or {}).values()
            for r in (rows if isinstance(rows, list) else [])
        )
        fit_ = edge_rig.c.learning._state.thermal_model_cache[f"cycle_fit_{mode}"]
        assert fit_["ne"] == 6
        assert fit_["coast_area_f"] * (1 if mode == "heat" else -1) > 0.1, "the room keeps going after the stop"


# ---------------------------------------------------------------------------------------------------------------
# 3. sensor lag
# ---------------------------------------------------------------------------------------------------------------


class TestSensorLag:
    @pytest.mark.parametrize(("q", "tol"), [(6.0, 0.05), (10.0, 0.06), (15.0, 0.08)])
    def test_maintenance_cycles_on_a_lagging_fine_sensor_are_unbiased(self, q, tol):
        """lagcheck.py: 2-min sensor lag, fine sensor, 1 F swing. Before: -8 / -22 / -37 % graded high."""
        m = 1.0 / (q - 0.04 * 28) * 60
        cyc = synth("heat", q=q, k=-0.04, res=0.1, n=30, run=(m * 0.85, m * 1.15), seed=5, lag=2.0)
        r = fit(cyc)
        assert abs(r["q"] - q) / q < tol
        assert r["lag_min"] == pytest.approx(2.0, abs=0.8)

    def test_cool_mirror(self):
        q = -10.0
        m = 1.0 / (10.0 - 0.04 * 20) * 60
        cyc = synth("cool", q=q, k=-0.04, res=0.1, n=30, run=(m * 0.85, m * 1.15), seed=6, lag=2.0)
        r = fit(cyc, "cool")
        assert abs(r["q"] - q) / abs(q) < 0.08

    def test_no_lag_still_exact(self):
        cyc = synth("heat", q=10.0, k=-0.04, res=0.1, n=30, run=(6.0, 8.0), seed=5, lag=0.0)
        r = fit(cyc)
        assert abs(r["q"] - 10.0) / 10.0 < 0.03
        assert r["lag_min"] < 0.6

    def test_lag_is_pooled_across_modes_on_the_same_sensor_only(self):
        heat = synth("heat", q=10.0, res=0.1, n=12, run=(6.0, 8.0), seed=8, lag=2.0)
        cool = synth("cool", q=-10.0, res=0.1, n=3, run=(6.0, 8.0), seed=9, lag=2.0)
        fh = fit(heat)
        alone = fit(cool, "cool")
        pooled = cm.fit_cycles(cool, "cool", cm.typical_prior("cool"), now_ts=0, lag_evidence=fh)
        assert pooled["lag_pooled"] is True and alone["lag_pooled"] is False
        assert pooled["lag_sd_min"] <= alone["lag_sd_min"] + 1e-9
        other = dict(fh, entity="sensor.other")
        assert (
            cm.fit_cycles(cool, "cool", cm.typical_prior("cool"), now_ts=0, lag_evidence=other)["lag_pooled"] is False
        )

    def test_unidentified_lag_caps_short_runs_at_medium(self):
        r = {"ne": 10, "er_q": 0.99, "resolution_f": 0.1, "lag_identified": False, "run_median_min": 5.0}
        assert cm.grade(r) == "medium" and cm.grade_caps(r) == ["lag_unidentified"]
        assert cm.grade(dict(r, run_median_min=30.0)) == "high"
        assert cm.grade(dict(r, lag_identified=True)) == "high"


# ---------------------------------------------------------------------------------------------------------------
# 4. honest grades: k pinned at the typical prior
# ---------------------------------------------------------------------------------------------------------------


class TestKPinnedGrade:
    def test_coarse_recovery_with_typical_k_grades_low_and_says_why(self):
        cyc = synth("heat", q=6.0, k=-0.04, res=1.0, n=20, run=(55.0, 65.0), seed=3)
        r = fit(cyc)
        assert r["k_source"] == "typical" and r["k_pinned"] is True
        assert r["k_dependent"] is True and r["grade"] == "low"
        assert "k_typical_prior" in r["grade_caps"]

    def test_learned_k_lifts_the_cap_and_is_accurate(self):
        """Owner's analysis: true k -0.15, k pinned at the typical -0.03 -> q ~50 % low; with the learned k 1-4 %."""
        cyc = synth("heat", q=6.0, k=-0.15, res=1.0, n=20, run=(55.0, 65.0), seed=3)
        typical = fit(cyc)
        cache = {"k_passive": -0.15, "observation_count_passive": 60}
        learned = cm.fit_cycles(cyc, "heat", cm.prior_from_cache(cache, "heat", 0), now_ts=0)
        assert learned["k_source"] == "learned" and learned["k_dependent"] is False
        assert abs(learned["q"] - 6.0) / 6.0 < 0.08
        assert abs(typical["q"] - 6.0) > abs(learned["q"] - 6.0)
        assert typical["grade"] == "low"

    def test_a_result_without_the_new_fields_grades_as_before(self):
        assert cm.grade({"ne": 4, "er_q": 0.96, "resolution_f": 0.1}) == "high"
        assert cm.grade({"ne": 2, "er_q": 0.96, "resolution_f": 1.0}) == "medium"


# ---------------------------------------------------------------------------------------------------------------
# 5. information weights
# ---------------------------------------------------------------------------------------------------------------


class TestInformationWeights:
    def test_equal_information_is_the_old_count_rule(self):
        w = cm.information_weights([1.0] * 30)
        assert w == pytest.approx([0.5 ** ((29 - i) / 20) for i in range(30)])

    def test_a_long_accurate_run_outlives_many_short_ones(self):
        long_info, short_info = 10.0, 0.5
        w = cm.information_weights([long_info] + [short_info] * 40)
        assert w[0] > 0.9, "40 short pulses carry 2x the long run's information: it keeps ~90 % of its weight"
        assert 0.5 ** (40 / 20) < 0.3  # the old count rule had it at a quarter

    def test_a_short_run_yields_quickly_to_a_long_one(self):
        w = cm.information_weights([0.5, 20.0])
        assert w[0] == pytest.approx(0.25)

    def test_contradiction_is_faster(self):
        infos = [1.0] * 11
        agree = cm.information_weights(infos)
        contra = cm.information_weights(infos, halves=[cm.EVIDENCE_HALF_N_CONTRADICT] + [cm.EVIDENCE_HALF_N] * 10)
        assert contra[0] < agree[0] and contra[1:] == pytest.approx(agree[1:])

    def test_long_runs_carry_more_information_than_short_pulses(self):
        short = fit(synth("heat", q=7.0, res=0.1, n=6, run=(5.0, 6.0), seed=4))
        long_ = fit(synth("heat", q=7.0, res=0.1, n=6, run=(40.0, 45.0), seed=4))
        assert min(long_["info_eligible"]) > 5 * max(short["info_eligible"])

    def test_the_cap_keeps_the_heaviest_cycles(self, monkeypatch):
        monkeypatch.setattr(cm, "CYCLE_MAX_CYCLES", 8)
        cyc = synth("heat", q=7.0, res=0.1, n=1, run=(45.0, 50.0), seed=11) + synth(
            "heat", q=7.0, res=0.1, n=10, run=(5.0, 6.0), seed=12
        )
        shift = cyc[0]["t_off"] + 3600 - cyc[1]["t_on"]
        for c in cyc[1:]:
            c["t_on"] += shift
            c["t_off"] += shift
            c["samples"] = [[s[0] + shift, s[1], s[2]] for s in c["samples"]]
            c["outdoor"] = [[o[0] + shift, o[1]] for o in c["outdoor"]]
        r = fit(cyc)
        assert r["ne"] == 8 and cyc[0]["t_on"] in r["t_eligible"], "the long accurate run is not pushed out"

    def test_keep_counts_new_information_not_new_cycles(self, tmp_path):
        """A short low-information cycle displaces the carried evidence only by its share of a typical cycle."""
        from custom_components.climate_advisor.learning import _new_information

        res = {"t_eligible": [1.0, 2.0, 3.0], "info_eligible": [1.0, 1.0, 0.1], "info_ref": 1.0}
        assert _new_information(res, [3.0], None) == pytest.approx(0.1)
        assert _new_information(res, [2.0, 3.0], None) == pytest.approx(1.1)
        assert _new_information(res, [3.0], 0.05) == pytest.approx(2.0)  # a held fit in its own (smaller) unit
        assert _new_information({"t_eligible": [1.0, 2.0]}, [2.0], None) == 1.0  # older result: count


# ---------------------------------------------------------------------------------------------------------------
# 6. whole / half-degree sensors are named
# ---------------------------------------------------------------------------------------------------------------


class TestCoarseSensorNotice:
    @pytest.mark.parametrize(("res", "cls"), [(1.0, "whole"), (0.5, "half"), (0.1, "fine"), (0.18, "fine")])
    def test_sensor_class(self, res, cls):
        cyc = synth("heat", q=6.0, res=res, n=8, run=(8.0, 12.0), seed=2)
        r = fit(cyc)
        assert r["sensor_class"] == cls
        notice = cm.coarse_sensor_notice(r, "heat")
        assert (notice is None) == (cls == "fine")

    @pytest.mark.parametrize(
        ("fit_", "in_use"),
        [
            ({"sensor_class": "whole", "n_cycles": 9, "ne": 0, "prior_source": "typical"}, "default"),
            ({"sensor_class": "whole", "n_cycles": 9, "ne": 0, "prior_source": "cache"}, "learned"),
            ({"sensor_class": "half", "n_cycles": 9, "ne": 3, "prior_source": "cache"}, "measured"),
            ({"resolution_f": 1.0, "n_cycles": 9, "ne": 0}, "default"),  # a fit stored before sensor_class existed
        ],
    )
    def test_notice_says_what_is_used_instead(self, fit_, in_use):
        n = cm.coarse_sensor_notice(fit_, "cool")
        assert n["rate_in_use"] == in_use and n["maintenance_cycles_learn"] is False
        assert "cooling" in n["text"] and "finer sensor" in n["text"] and "setback" in n["text"]

    def test_rows_expose_it_for_analysis_debug_and_ai(self, tmp_path):
        r = _Rig(tmp_path)
        try:
            r.c.learning._state.thermal_model_cache["cycle_fit_heat"] = {
                "q": 8.0,
                "grade": "none",
                "ne": 0,
                "n_cycles": 12,
                "resolution_f": 1.0,
                "prior_source": "typical",
            }
            (row,) = r.c._cycle_model_rows("fahrenheit")
            assert row["coarse_sensor"]["sensor_class"] == "whole"
            assert row["coarse_sensor"]["rate_in_use"] == "default"
        finally:
            r.close()

    def test_ai_context_line(self):
        from custom_components.climate_advisor.ai_skills_context import build_thermal_pipeline_context
        from tests.test_ai_skills_context_learning_health import _make_coordinator

        coord = _make_coordinator({})
        coord.config = {"temp_unit": "fahrenheit"}
        notice = cm.coarse_sensor_notice({"sensor_class": "whole", "n_cycles": 5, "ne": 0}, "heat")
        coord.decision_rates_summary.return_value = [
            {
                "key": "cycle_heat",
                "source": "cycle",
                "value": {"q": 8.0, "grade": "none", "ne": 0, "lag_min": 2.0, "lag_identified": False},
                "coarse_sensor": notice,
            },
        ]
        coord.decision_rates_summary.return_value[0]["value"]["coarse_sensor"] = notice
        ctx = asyncio.run(build_thermal_pipeline_context(MagicMock(), coord))
        assert "cycle model heat sensor: Your indoor temperature sensor reports whole degrees" in ctx
        assert "(rate in use: default)" in ctx and "sensor lag 2.0 min (not identified)" in ctx


# ---------------------------------------------------------------------------------------------------------------
# 7. upgrade continuity (0.9.31 -> 0.9.32)
# ---------------------------------------------------------------------------------------------------------------


class TestUpgradeContinuity:
    def _v1_fit(self, cyc):
        r = fit(cyc)
        old = {k: r[k] for k in ("q", "sd_q", "ne", "n_cycles", "er_q", "grade", "resolution_f", "entity")}
        old.update({"t_first": r["t_first"], "t_last": r["t_last"], "t_seen": r["t_last"], "applied": True})
        old["q"] = 7.7  # what 0.9.31's model said (lag-biased)
        return old

    def test_startup_refit_of_the_same_cycles_moves_nothing(self, tmp_path):
        cyc = synth("heat", q=10.0, res=0.1, n=10, run=(6.0, 8.0), seed=5)
        e = LearningEngine(tmp_path)
        e.load_state()
        old = self._v1_fit(cyc)
        e._state.thermal_model_cache = {"k_active_heat": 7.7, "cycle_fit_heat": dict(old), "k_passive": -0.04}
        before = {k: v for k, v in e._state.thermal_model_cache.items()}
        assert e.apply_cycle_fit("heat", fit(cyc), primary_is_v3=True) is False
        after = e._state.thermal_model_cache
        assert after["cycle_fit_heat"] == before["cycle_fit_heat"] and after["k_active_heat"] == 7.7
        assert e.get_thermal_model()["heating_rate_f_per_hour"] == 7.7

    def test_the_first_new_cycle_brings_the_new_model(self, tmp_path):
        cyc = synth("heat", q=10.0, res=0.1, n=11, run=(6.0, 8.0), seed=5)
        e = LearningEngine(tmp_path)
        e.load_state()
        e._state.thermal_model_cache = {
            "k_active_heat": 7.7,
            "cycle_fit_heat": self._v1_fit(cyc[:10]),
            "k_passive": -0.04,
        }
        new = fit(cyc)
        assert e.apply_cycle_fit("heat", new, primary_is_v3=True) is True
        rec = e._state.thermal_model_cache["cycle_fit_heat"]
        assert rec["model_version"] == cm.MODEL_VERSION and rec["q"] == pytest.approx(new["q"])

    def test_old_cycles_and_old_fits_still_load_and_fit(self):
        """0.9.31 cycles carry no new fields; old fits have no lag/info: everything reads them."""
        cyc = synth("heat", q=7.0, res=1.0, n=5, run=(40.0, 50.0), seed=1)
        for c in cyc:
            c.pop("res", None)
        r = fit(cyc)
        assert r["model_version"] == cm.MODEL_VERSION and r["grade"] in ("low", "medium", "none")
        assert cm.grade({"ne": 3, "er_q": 0.99, "resolution_f": 1.0}) == "medium"
        assert cm.prior_from_cache({"cycle_fit_heat": {"q": 7.0}}, "heat", 0)["k_source"] == "typical"


# ---------------------------------------------------------------------------------------------------------------
# 8. evidence-based retention of the cycle file (owner 2026-11-12: "wouldn't want to age the data out if it was
#    more correct")
# ---------------------------------------------------------------------------------------------------------------


def _spread(cycles: list[dict], start: float, every_days: float) -> list[dict]:
    """Move each cycle to ``start + i * every_days`` (keeps its internal timing)."""
    out = []
    for i, c in enumerate(cycles):
        shift = start + i * every_days * 86400.0 - c["t_on"]
        d = dict(c)
        d["id"] = f"{c['mode']}-{int(c['t_on'] + shift)}"
        d["t_on"] = c["t_on"] + shift
        d["t_off"] = c["t_off"] + shift
        d["samples"] = [[s[0] + shift, s[1], s[2]] for s in c["samples"]]
        d["outdoor"] = [[o[0] + shift, o[1]] for o in c["outdoor"]]
        out.append(d)
    return out


def _store_after_fit(tmp_path, cycles: list[dict], mode: str):
    from custom_components.climate_advisor.cycle_store import CycleStore

    store = CycleStore(tmp_path / "cycles.json")
    for c in cycles:
        store.add(c, now=cycles[-1]["t_off"])
    r = cm.fit_cycles(store.cycles(mode), mode, cm.typical_prior(mode), now_ts=0)
    store.set_weights(r["w_by_id"])
    store.prune(cycles[-1]["t_off"] + 3600, force=True)
    return store, r


class TestEvidenceRetention:
    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_a_long_accurate_run_survives_90_days_of_short_pulses(self, tmp_path, mode):
        sgn = 1.0 if mode == "heat" else -1.0
        t0 = 1_760_000_000.0
        long_run = _spread(synth(mode, q=7.0 * sgn, res=0.1, n=1, run=(45.0, 50.0), seed=31), t0, 1)
        pulses = _spread(synth(mode, q=7.0 * sgn, res=0.1, n=100, run=(5.0, 6.0), seed=32), t0 + 86400.0, 1.2)
        store, r = _store_after_fit(tmp_path, long_run + pulses, mode)
        ids = {c["id"] for c in store.cycles(mode)}
        assert pulses[-1]["t_on"] - long_run[0]["t_on"] > 110 * 86400
        assert long_run[0]["id"] in ids, "an accurate long run that still carries weight is never aged out"
        assert r["w_by_id"][long_run[0]["id"]] > 0.3

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_a_contradicted_run_is_evicted_an_agreeing_one_kept(self, tmp_path, mode):
        sgn = 1.0 if mode == "heat" else -1.0
        t0 = 1_760_000_000.0
        newer = _spread(synth(mode, q=7.0 * sgn, res=0.1, n=40, run=(40.0, 45.0), seed=33), t0 + 86400.0, 3.0)
        for q_old, kept in ((3.0, False), (7.0, True)):
            old = _spread(synth(mode, q=q_old * sgn, res=0.1, n=1, run=(40.0, 45.0), seed=34), t0, 1)
            store, r = _store_after_fit(tmp_path / f"q{q_old}", old + newer, mode)
            ids = {c["id"] for c in store.cycles(mode)}
            assert (old[0]["id"] in ids) is kept, (q_old, r["w_by_id"][old[0]["id"]])

    def test_the_cap_evicts_the_lowest_weight_not_the_oldest(self, tmp_path):
        from custom_components.climate_advisor.cycle_store import CycleStore

        store = CycleStore(tmp_path / "c.json", max_cycles=8)  # 4 per mode
        cyc = _spread(synth("heat", q=7.0, res=0.1, n=4, run=(8.0, 9.0), seed=35), 1_760_000_000.0, 1)
        for c in cyc:
            store.add(c, now=cyc[-1]["t_off"])
        store.set_weights({cyc[0]["id"]: 0.9, cyc[1]["id"]: 0.05, cyc[2]["id"]: 0.5, cyc[3]["id"]: 1.0})
        extra = _spread(synth("heat", q=7.0, res=0.1, n=1, run=(8.0, 9.0), seed=36), cyc[-1]["t_on"] + 86400, 1)
        store.add(extra[0], now=extra[0]["t_off"])  # unweighed (just recorded): full weight
        ids = [c["id"] for c in store.cycles("heat")]
        assert cyc[1]["id"] not in ids and cyc[0]["id"] in ids and extra[0]["id"] in ids

    def test_weights_survive_a_reload(self, tmp_path):
        from custom_components.climate_advisor.cycle_store import CycleStore

        p = tmp_path / "c.json"
        store = CycleStore(p)
        cyc = _spread(synth("cool", q=-7.0, res=0.1, n=2, run=(8.0, 9.0), seed=37), 1_760_000_000.0, 1)
        for c in cyc:
            store.add(c, now=cyc[-1]["t_off"])
        store.set_weights({cyc[0]["id"]: 0.004})
        assert store.save()
        again = CycleStore(p)
        again.load(now=cyc[-1]["t_off"])
        assert again._by_id[cyc[0]["id"]].w == pytest.approx(0.004)
        assert again._by_id[cyc[1]["id"]].w is None


class TestFirstLeadInAfterInstall:
    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_first_ever_lead_in_counts_from_the_burner_and_records_no_rejection(self, mode):
        coord = _make_thermostat_coord()

        def _start(m):  # the real start's contract: a still-pending observation is abandoned (rejected) first
            key = f"hvac_{m}"
            if key in coord._pending_observations:
                coord._abandon_observation(key, "new HVAC session started")
            coord._pending_observations[key] = {"_phase": "active"}

        coord._start_hvac_observation = AsyncMock(side_effect=_start)
        coord._abandon_observation = MagicMock()
        coord._pending_observations = {}
        coord._ensure_pending_observations = lambda: None
        act = _ACTION[mode]
        _fire(coord, _t(0), (mode, "idle"), (mode, "fan"))  # never seen heating yet: #93 fallback opens it
        _fire(coord, _t(1), (mode, "fan"), (mode, act))
        _fire(coord, _t(11), (mode, act), (mode, "idle"))
        assert coord._today_record.hvac_runtime_minutes == pytest.approx(10.0)
        coord._abandon_observation.assert_not_called()
