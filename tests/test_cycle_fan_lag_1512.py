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
        assert coord._thermostat_has_reported_runs() is True

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

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    @pytest.mark.parametrize("action", ["", "fan"])
    def test_a_thermostat_that_never_reports_runs_has_no_runs(self, mode, action):
        """Owner decision 2026-11-13 ("Never, remove it."): the #93 mode fallback is gone. A thermostat that reports
        only ``fan`` (or no hvac_action) while its mode is heat/cool records no run, no runtime, no observation. This
        test replaced ``test_thermostat_never_seen_heating_keeps_the_93_fallback``, whose expectation (50 min of
        runtime from a selected mode) is now wrong by that decision."""
        coord = _make_thermostat_coord()
        _fire(coord, _t(0), ("off", action), (mode, action))
        _fire(coord, _t(50), (mode, action), ("off", action))
        assert coord._today_record.hvac_runtime_minutes == 0.0
        coord._start_hvac_observation.assert_not_called()
        assert coord._thermostat_has_reported_runs() is False

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_heating_reported_runs_as_before(self, mode):
        coord = _make_thermostat_coord()
        _fire(coord, _t(0), (mode, "idle"), (mode, _ACTION[mode]))
        _fire(coord, _t(12), (mode, _ACTION[mode]), (mode, "idle"))
        assert coord._today_record.hvac_runtime_minutes == pytest.approx(12.0)
        coord._start_hvac_observation.assert_called_once_with(mode)

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
        # owner decision 2026-11-13: only a reported heating/cooling is a run; fan_only stays the fan session (#912)
        assert ha.thermostat_reports_running("heat", "fan", ca_ventilating=False) is False
        assert ha.thermostat_reports_running("heat", "", ca_ventilating=False) is False
        assert ha.thermostat_reports_running("fan_only", "fan", ca_ventilating=False) is True
        assert ha.thermostat_reports_running("fan_only", "fan", ca_ventilating=True) is False  # #986
        assert ha.thermostat_reports_running("cool", "cooling", ca_ventilating=False) is True

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

    def test_a_contradicted_light_pulse_never_outlasts_an_agreeing_one(self):
        infos = [0.1] + [1.0] * 6  # a 5-minute pulse, then 6 typical runs
        agree = cm.information_weights(infos)
        contra = cm.information_weights(infos, contradicted=[True] + [False] * 6)
        assert contra[0] <= agree[0]
        heavy = cm.information_weights([125.0] + [1.0] * 20, contradicted=[True] + [False] * 20)
        assert heavy[0] < 0.01  # and a heavy contradicted run still yields in absolute terms

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
        """New evidence is counted in reference cycles (fixed unit, no pool median): a short pulse a fraction, a long
        run more but never more than CYCLE_INFO_MAX (verification 2026-11-13: one 60-min run counted as 397 cycles
        and wiped the carried legacy evidence)."""
        from custom_components.climate_advisor.learning import _new_information

        res = {"t_eligible": [1.0, 2.0, 3.0], "info_eligible": [1.0, 125.0, 0.1], "info_ref": 1.0}
        assert _new_information(res, [3.0]) == pytest.approx(0.1)
        assert _new_information(res, [2.0]) == pytest.approx(cm.CYCLE_INFO_MAX)  # capped
        assert _new_information(res, [1.0, 3.0]) == pytest.approx(1.1)
        assert _new_information(dict(res, info_ref=50.0), [1.0, 3.0]) == pytest.approx(1.1)  # neighbours don't matter
        assert _new_information({"t_eligible": [1.0, 2.0]}, [2.0]) == 1.0  # older result: count

    def test_information_does_not_depend_on_the_reporting_rate(self):
        """The same run read every 15 s or every minute carries the same information (time-weighted)."""
        one = synth("heat", q=7.0, res=0.01, n=3, run=(10.0, 10.0), seed=40, lag=0.0, noise=0.01)
        fast = []
        for c in one:  # interpolate 4 readings a minute
            d = dict(c)
            ss = c["samples"]
            d["samples"] = [
                [a[0] + (b[0] - a[0]) * f / 4, a[1] + (b[1] - a[1]) * f / 4, a[2]]
                for a, b in zip(ss, ss[1:], strict=False)
                for f in range(4)
            ] + [ss[-1]]
            fast.append(d)
        i1, i4 = fit(one)["info_eligible"], fit(fast)["info_eligible"]
        assert i1 == pytest.approx(i4, rel=0.05)
        assert 0.5 < i1[0] < 2.0  # a 10-minute run is about one reference cycle

    def test_one_long_run_cannot_wipe_the_carried_legacy_evidence(self, tmp_path):
        e = LearningEngine(tmp_path)
        e.load_state()
        e._state.thermal_model_cache = {"k_active_heat": 6.0, "observation_count_heat": 120, "k_passive": -0.04}
        long_run = fit(synth("heat", q=6.2, res=0.1, n=1, run=(60.0, 60.0), seed=41, lag=0.0))
        assert long_run["info_eligible"][0] > 50
        e.apply_cycle_fit("heat", long_run, primary_is_v3=True)
        keep = e._state.thermal_model_cache["rate_prior"]["k_active_heat"]["keep"]
        assert keep >= 0.5 ** (cm.CYCLE_INFO_MAX / cm.EVIDENCE_HALF_N) - 1e-6  # at most 5 reference cycles' worth


def _heavy_then_short(mode: str, q_old: float, q_new: float, n: int) -> list[dict]:
    sgn = 1.0 if mode == "heat" else -1.0
    old = synth(mode, q=q_old * sgn, n=1, run=(60.0, 60.0), seed=7, lag=0.0)
    new = synth(mode, q=q_new * sgn, n=n, run=(8.0, 10.0), seed=8, lag=0.0)
    off = old[0]["t_off"] + 3600 - new[0]["t_on"]
    for i, c in enumerate(new):
        c["t_on"] += off
        c["t_off"] += off
        c["id"] = f"{mode}-new{i}"
        c["samples"] = [[s_[0] + off, s_[1], s_[2]] for s_ in c["samples"]]
        c["outdoor"] = [[o[0] + off, o[1]] for o in c["outdoor"]]
    old[0]["id"] = f"{mode}-old"
    return old + new


class TestContradictedHeavyRun:
    """Verification 2026-11-13 (blocker): a long run the house no longer matches must be displaced quickly (main: 6.03
    after 80 runs; the relative rule kept 12.0, weight 0.94, graded high), while an agreeing long run keeps its
    weight."""

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_contradicted_heavy_run_yields_within_20_short_runs(self, mode):
        r = fit(_heavy_then_short(mode, 12.0, 6.0, 20), mode)
        assert abs(abs(r["q"]) - 6.0) / 6.0 < 0.06, r["q"]
        assert r["w_by_id"][f"{mode}-old"] < 0.01

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    @pytest.mark.parametrize("noise", [None, 0.2])
    def test_a_heavy_run_cannot_hide_its_own_contradiction(self, mode, noise):
        """Re-check 2026-11-13: the heavy run dominated the first fit, so the newer runs were judged against it and
        (noisy sensor) q stayed 12.0 "high" after 12-20 newer runs at 6. The pool-level check fits the newer runs on
        their own: while the contradicted run still holds evidence the grade is low, and q follows the new runs."""
        sgn = 1.0 if mode == "heat" else -1.0
        old = synth(mode, q=12.0 * sgn, n=1, run=(60.0, 60.0), seed=7, lag=0.0, noise=noise)
        for n, settled in ((12, False), (25, True)):
            new = synth(mode, q=6.0 * sgn, n=n, run=(8.0, 10.0), seed=8, lag=0.0, noise=noise)
            off = old[0]["t_off"] + 3600 - new[0]["t_on"]
            for i, c in enumerate(new):
                c["t_on"] += off
                c["t_off"] += off
                c["id"] = f"{mode}-n{i}"
                c["samples"] = [[s_[0] + off, s_[1], s_[2]] for s_ in c["samples"]]
                c["outdoor"] = [[o[0] + off, o[1]] for o in c["outdoor"]]
            old[0]["id"] = f"{mode}-old"
            r = fit(old + new, mode)
            assert r["grade"] != "high" or abs(abs(r["q"]) - 6.0) / 6.0 < 0.1, (n, r["q"], r["grade"])
            if settled:
                assert abs(abs(r["q"]) - 6.0) / 6.0 < 0.1, (n, r["q"])

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_agreeing_heavy_run_keeps_its_weight(self, mode):
        r = fit(_heavy_then_short(mode, 6.0, 6.0, 40), mode)
        assert r["w_by_id"][f"{mode}-old"] > 0.9
        assert abs(abs(r["q"]) - 6.0) / 6.0 < 0.05

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_retention_evicts_the_contradicted_heavy_run(self, tmp_path, mode):
        from custom_components.climate_advisor.cycle_store import CycleStore

        cyc = _heavy_then_short(mode, 12.0, 6.0, 30)
        shifted = _spread(cyc, 1_760_000_000.0, 4.0)  # spread over 116 days
        store = CycleStore(tmp_path / "c.json")
        for c in shifted:
            store.add(c, now=shifted[-1]["t_off"])
        r = cm.fit_cycles(store.cycles(mode), mode, cm.typical_prior(mode), now_ts=0)
        store.set_weights(r["w_by_id"])
        store.prune(shifted[-1]["t_off"] + 3600, force=True)
        ids = {c["id"] for c in store.cycles(mode)}
        assert shifted[0]["id"] not in ids and shifted[-1]["id"] in ids


# ---------------------------------------------------------------------------------------------------------------
# 6. whole / half-degree sensors are named
# ---------------------------------------------------------------------------------------------------------------


class TestCoarseSensorNotice:
    @pytest.mark.parametrize(
        ("res", "cls"),
        [(1.0, "whole"), (0.5, "half"), (0.1, "fine"), (0.18, "fine"), (0.9, "half_c"), (1.8, "whole_c")],
    )
    def test_sensor_class(self, res, cls):
        cyc = synth("heat", q=6.0, res=res, n=8, run=(8.0, 12.0), seed=2)
        r = fit(cyc)
        assert r["sensor_class"] == cls
        notice = cm.coarse_sensor_notice(r, "heat")
        assert (notice is None) == (cls == "fine")

    @pytest.mark.parametrize(
        ("fit_", "in_use"),
        [
            ({"resolution_f": 1.0, "n_cycles": 9, "ne": 0, "prior_source": "typical"}, "default"),
            ({"resolution_f": 1.0, "n_cycles": 9, "ne": 0, "prior_source": "cache"}, "learned"),
            ({"resolution_f": 0.5, "n_cycles": 9, "ne": 3, "prior_source": "cache"}, "measured"),
            ({"sensor_class": "whole", "n_cycles": 9, "ne": 0}, "default"),  # a stored fit without resolution_f
        ],
    )
    def test_notice_says_what_is_used_instead(self, fit_, in_use):
        n = cm.coarse_sensor_notice(fit_, "cool")
        assert n["rate_in_use"] == in_use and n["maintenance_cycles_learn"] is False
        assert "cooling" in n["text"] and "finer sensor" in n["text"] and "setback" in n["text"]
        if fit_.get("ne"):
            assert "3 runs that moved it further have taught it" in n["text"] and "can't teach" not in n["text"]

    def test_unit_aware_labels_and_v4(self):
        """A 0.5 C sensor (0.9 F steps) is "half a degree Celsius", not "whole degrees"; on a v4 zone the rate in use
        is v4's (verification 2026-11-13)."""
        half_c = cm.coarse_sensor_notice({"resolution_f": 0.9, "n_cycles": 4, "ne": 0}, "heat")
        assert half_c["sensor_class"] == "half_c" and "reports in half-degree Celsius steps" in half_c["text"]
        whole_c = cm.coarse_sensor_notice({"resolution_f": 1.8, "n_cycles": 4, "ne": 0}, "heat")
        assert "reports in whole degrees Celsius" in whole_c["text"]
        not_ready = cm.coarse_sensor_notice(
            {"resolution_f": 1.0, "n_cycles": 4, "ne": 1, "prior_source": "typical"}, "heat", fit_applied=False
        )
        assert not_ready["rate_in_use"] == "default"  # v4 still learning, nothing learned before: not the cycle fit
        one = cm.coarse_sensor_notice({"resolution_f": 1.0, "n_cycles": 4, "ne": 1}, "heat")
        assert "1 run that moved it further has taught it" in one["text"] and "that run measured" in one["text"]
        v4 = cm.coarse_sensor_notice({"resolution_f": 1.0, "n_cycles": 4, "ne": 0}, "heat", decisions="v4")
        assert v4["rate_in_use"] == "v4" and "the v4 engine's own heating rate" in v4["text"]

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
        notice = cm.coarse_sensor_notice({"resolution_f": 1.0, "n_cycles": 5, "ne": 0}, "heat")
        # the production row shape (coordinator._cycle_model_rows): every field on the row itself
        coord.decision_rates_summary.return_value = [
            {
                "key": "cycle_heat",
                "mode": "heat",
                "kind": "cycle",
                "source": "cycle",
                "q": 8.0,
                "grade": "none",
                "ne": 0,
                "lag_min": 2.0,
                "lag_identified": False,
                "coarse_sensor": notice,
            },
        ]
        ctx = asyncio.run(build_thermal_pipeline_context(MagicMock(), coord))
        assert "cycle model heat sensor: Your indoor temperature sensor reports in whole degrees" in ctx
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


# ---------------------------------------------------------------------------------------------------------------
# 9. verification 2026-11-13: the overrun link, the lead-in blower (#1535), the un-learnable action flag
# ---------------------------------------------------------------------------------------------------------------


class TestOverrunLinkIsShort:
    def test_ecobee_one_second_gap_is_linked(self):
        coord = _make_thermostat_coord()
        coord._derive_thermostat_fan_running_for_reconcile = MagicMock(return_value=True)
        _fire(coord, _t(0), ("heat", "idle"), ("heat", "heating"))
        _fire(coord, _t(8), ("heat", "heating"), ("heat", "idle"))
        _fire(coord, _t(8 + 1 / 60), ("heat", "idle"), ("heat", "fan"))
        coord.automation_engine.note_observed_fan_origin.assert_any_call("post_heat_tail")
        kw = coord.automation_engine.reconcile_fan_on_startup.await_args.kwargs
        assert kw["recent_hvac_session_ended"] is True and kw["thermostat_blower_phase"] is True

    def test_a_fan_seven_minutes_after_the_stop_is_not_the_overrun(self):
        """A schedule fan 7 min after a stop is not the run's overrun (was linked with a 600 s window)."""
        coord = _make_thermostat_coord()
        coord._derive_thermostat_fan_running_for_reconcile = MagicMock(return_value=True)
        with patch("custom_components.climate_advisor.coordinator.async_call_later"):
            _fire(coord, _t(0), ("heat", "idle"), ("heat", "heating"))
            _fire(coord, _t(8), ("heat", "heating"), ("heat", "idle"))
            _fire(coord, _t(15), ("heat", "idle"), ("heat", "fan"))
        coord.automation_engine.note_observed_fan_origin.assert_called_with("thermostat_schedule")
        assert coord._overrun_fan_linked is False


class TestLeadInBlowerIsNeverAdopted:
    """Issue #1535 (mirror of #1534 on the lead-in side), heat and cool."""

    def _lead_in(self, mode, *, run_follows: bool):
        coord = _make_thermostat_coord()
        coord._derive_thermostat_fan_running_for_reconcile = MagicMock(return_value=True)
        act = _ACTION[mode]
        later = []
        with patch(
            "custom_components.climate_advisor.coordinator.async_call_later",
            side_effect=lambda hass, delay, cb: later.append((delay, cb)),
        ):
            _fire(coord, _t(0), (mode, "idle"), (mode, act))  # the thermostat reports its compressor
            _fire(coord, _t(5), (mode, act), (mode, "idle"))
            _fire(coord, _t(60), (mode, "idle"), (mode, "fan"))
            if run_follows:
                _fire(coord, _t(61), (mode, "fan"), (mode, act))
        rec = coord.automation_engine.reconcile_fan_on_startup
        assert rec.await_count == 0, "an unowned idle -> fan is not reconciled at once on a reporting thermostat"
        (delay, cb), *_ = [x for x in later if x[0] == 120]
        coord.hass.states.get = MagicMock(return_value=_make_state(mode, act if run_follows else "fan"))
        with patch(_DT) as mock_dt:
            mock_dt.now.return_value = _t(62)
            asyncio.run(cb(None))
        return rec

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_a_lead_in_followed_by_a_run_is_never_reconciled(self, mode):
        rec = self._lead_in(mode, run_follows=True)
        assert rec.await_count == 0

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_a_fan_with_no_run_after_it_is_still_reconciled(self, mode):
        rec = self._lead_in(mode, run_follows=False)
        assert rec.await_count == 1
        assert rec.await_args.kwargs.get("thermostat_blower_phase", False) is False

    def test_reconcile_never_adopts_the_blower_phase(self):
        from tests.test_fan_control import TestReconcileFanOnStartup

        t = TestReconcileFanOnStartup()
        for blower_phase, adopted in ((True, False), (False, True)):
            engine = t._engine()
            engine._emit_event_callback = MagicMock()
            asyncio.run(
                engine.reconcile_fan_on_startup(
                    indoor=75.0,
                    outdoor=65.0,
                    thermostat_fan_running=True,
                    any_sensor_open=True,
                    thermostat_blower_phase=blower_phase,
                )
            )
            assert engine._natural_vent_active is adopted


class TestAFanIsNeverInferredToBeARun:
    """Owner decision 2026-11-13: "just because running the fan warms the house doesn't mean the heater is running.
    If we don't get a heating status from the furnace we shouldn't try to guess that it's a heating run. It could just
    be fan on a hot day." A fan, however long and however the room moves, is never a heating or cooling run on a
    thermostat that reports its burner/compressor (the un-learn rule of the first verification round is removed)."""

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    @pytest.mark.parametrize("ca_fan", [None, "fan", "nat_vent"])
    def test_a_long_fan_while_the_room_moves_records_nothing(self, mode, ca_fan):
        coord = _make_thermostat_coord()
        act = _ACTION[mode]
        _fire(coord, _t(0), (mode, "idle"), (mode, act))  # the thermostat reports its burner / compressor
        _fire(coord, _t(5), (mode, act), (mode, "idle"))
        coord._start_hvac_observation.reset_mock()
        before = coord._today_record.hvac_runtime_minutes
        if ca_fan == "fan":
            coord.automation_engine._fan_active = True
        elif ca_fan == "nat_vent":
            coord.automation_engine._natural_vent_active = True
        with patch("custom_components.climate_advisor.coordinator.async_call_later"):
            _fire(coord, _t(60), (mode, "idle"), (mode, "fan"))  # fan "on" on a sunny afternoon / evening
            for minute in range(65, 125, 5):  # an hour of fan, the room moving the mode's way all along
                _fire(coord, _t(minute), (mode, "fan"), (mode, "fan"))
            _fire(coord, _t(125), (mode, "fan"), (mode, "idle"))
        assert coord._today_record.hvac_runtime_minutes == before
        coord._start_hvac_observation.assert_not_called()
        assert coord._hvac_session_mode is None and coord._thermostat_has_reported_runs() is True
        assert not hasattr(coord, "_check_unlearn_reports_action")

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_no_cycle_from_a_long_fan(self, edge_rig, mode):
        r = edge_rig
        act = _ACTION[mode]
        sgn = 1.0 if mode == "heat" else -1.0
        r.at(0)
        _edge(r, _state(mode, "idle"), _state(mode, act))
        r.at(5)
        _edge(r, _state(mode, act), _state(mode, "idle"))
        r.at(50)
        r.c._cycle_tick()
        r.drain()
        n0 = len(r.c._cycle_store.cycles(mode))
        r.at(60)
        _edge(r, _state(mode, "idle"), _state(mode, "fan"))
        for i in range(1, 121):  # an hour of fan, the room moving 1.5 F the mode's way
            r.at(60 + i * 0.5)
            r.indoor = round(68.0 + sgn * 1.5 * i / 120, 2)
            r.c._cycle_reading()
        r.at(121)
        _edge(r, _state(mode, "fan"), _state(mode, "idle"))
        r.at(200)
        r.c._cycle_tick()
        r.drain()
        assert len(r.c._cycle_store.cycles(mode)) == n0
        assert not r.c._cycle_recorder.open_modes

    def test_the_report_watch_belongs_to_the_climate_entity(self):
        coord = _make_thermostat_coord()
        _fire(coord, _t(0), ("heat", "idle"), ("heat", "heating"))
        assert coord._thermostat_has_reported_runs() is True
        coord.config = {**coord.config, "climate_entity": "climate.other"}
        assert coord._thermostat_has_reported_runs() is False  # another thermostat starts unknown


def test_a_sub_hundredth_step_sensor_never_has_zero_resolution():
    """Found in the verification round: steps under 0.005 F rounded to a 0.0 resolution and the fit divided by it."""
    assert cm.infer_resolution([70.0, 70.001, 70.002, 70.004]) == 0.01
    from custom_components.climate_advisor.cycle_recorder import _infer_step

    assert _infer_step([70.0, 70.001, 70.002]) == 0.01


class TestThermostatThatNeverReportsRuns:
    """Owner decision 2026-11-13: a run exists only when the thermostat reports heating/cooling. A thermostat that
    never does gets no runtime, observations, cycles or rate learning, and, after 3 days in a heating/cooling mode,
    an occupant notice."""

    def _tick_days(self, coord, days: float, state):
        coord.hass.states.get = MagicMock(return_value=state)
        start = datetime(2026, 11, 1, 0, 0, 0)
        for k in range(int(days * 24 * 12) + 1):
            with patch(_DT) as mock_dt:
                mock_dt.now.return_value = start + __import__("datetime").timedelta(minutes=5 * k)
                coord._tick_hvac_action_watch()

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    @pytest.mark.parametrize("action", ["", "fan"])
    def test_notice_after_three_days_in_a_mode_with_no_report(self, mode, action):
        coord = _make_thermostat_coord()
        self._tick_days(coord, 2.9, _make_state(mode, action))
        assert coord.thermostat_action_notice() is None  # not before N (3 days of heat/cool-mode time)
        self._tick_days(coord, 0.2, _make_state(mode, action))
        n = coord.thermostat_action_notice()
        assert n is not None and n["rate_in_use"] == "default" and n["ever_reported"] is False
        assert "has not reported when it is heating or cooling" in n["text"] and "never counted as a run" in n["text"]
        assert coord.thermostat_action_notice("v4")["rate_in_use"] == "v4"

    def test_off_mode_time_does_not_count_and_a_report_clears_it(self):
        coord = _make_thermostat_coord()
        self._tick_days(coord, 4.0, _make_state("off", ""))
        assert coord.thermostat_action_notice() is None
        self._tick_days(coord, 3.2, _make_state("heat", ""))
        assert coord.thermostat_action_notice() is not None
        _fire(coord, _t(0), ("heat", "idle"), ("heat", "heating"))
        assert coord.thermostat_action_notice() is None

    def test_no_cost_estimate_for_a_thermostat_with_no_runs(self):
        from custom_components.climate_advisor.learning import DailyRecord

        coord = _make_thermostat_coord()
        coord.config = {**coord.config, "energy_rate": 0.2, "heat_power_kw": 3.0, "cool_power_kw": 3.0}
        rec = DailyRecord(date="2026-11-04", day_type="cold", trend_direction="stable")
        coord._apply_estimated_cost(rec)
        assert rec.estimated_cost == 0.0  # a reporting (or not yet judged) thermostat: nothing ran costs 0
        self._tick_days(coord, 3.2, _make_state("heat", "fan"))
        coord._apply_estimated_cost(rec)
        assert rec.estimated_cost is None  # unknown, never a 0.00 that reads as "nothing ran"


def test_ai_context_names_a_thermostat_that_never_reports_runs():
    from custom_components.climate_advisor.ai_skills_context import build_thermal_pipeline_context
    from tests.test_ai_skills_context_learning_health import _make_coordinator

    coord = _make_coordinator({})
    coord.config = {"temp_unit": "fahrenheit"}
    coord.decision_rates_summary.return_value = [
        {
            "key": "thermostat_reports",
            "kind": "notice",
            "source": "notice",
            "value": None,
            "rate_in_use": "default",
            "ever_reported": False,
            "text": "Your thermostat has not reported when it is heating or cooling at all",
        }
    ]
    ctx = asyncio.run(build_thermal_pipeline_context(MagicMock(), coord))
    assert "thermostat: Your thermostat has not reported when it is heating or cooling at all" in ctx
