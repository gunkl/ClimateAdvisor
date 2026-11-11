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
from custom_components.climate_advisor.thermal_confidence import (  # noqa: E402
    grade_hvac_confidence,
    hvac_evidence_count,
)

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
        # Issue #1512 finding 2: the legacy counter is no longer overwritten with ne (5), which dropped an established
        # home's confidence on its first post-upgrade cycles; the model reports the evidence (40 carried + 5 cycles)
        assert c[f"observation_count_{mode}"] == 40
        model = e.get_thermal_model()
        assert model[f"observation_count_{mode}"] == 45
        # the fit's own grade is medium, but the carried evidence it agrees with justifies high (was "medium": the
        # old rule graded an upgraded home only by the er_q against its own strong prior)
        assert model["confidence"] == "high"
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

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_a_fit_with_no_eligible_cycle_never_overwrites_the_rates_or_the_confidence(self, tmp_path, mode):
        """Grade none (ne 0, the prior returned unchanged): stored for display, not applied even on v3, and the
        confidence the mode's own carried evidence gives is kept. The old version of this test only passed because
        the OTHER mode's untouched count (40) still graded the home; a heat-only home fell to none."""
        e = LearningEngine(tmp_path)
        e.load_state()
        field = f"k_active_{mode}"
        e._state.thermal_model_cache = {"k_passive": -0.05, field: 3.0 if mode == "heat" else -3.5}
        e._state.thermal_model_cache[f"observation_count_{mode}"] = 40
        before = grade_hvac_confidence(dict(e._state.thermal_model_cache))
        assert before == "high"
        q = 8.0 if mode == "heat" else -8.0
        assert e.apply_cycle_fit(mode, _fit(q=q, grade="none", ne=0), primary_is_v3=True) is False
        c = e._state.thermal_model_cache
        assert c[field] == (3.0 if mode == "heat" else -3.5)
        assert c[f"observation_count_{mode}"] == 40
        assert grade_hvac_confidence(c) == before == e.get_thermal_model()["confidence"]

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

    def test_v4_fit_grades_like_v3_one_rule(self):
        """Owner decision 2026-11-11 (c): one rule for v3 and v4: the fit's grade speaks whether or not its q drives
        the decisions (was: a v4 zone ignored the grade and counted the cycles, so a whole-degree home's own cycles
        could reach "high" there)."""
        for applied in (True, False):
            cache = {"cycle_fit_heat": {"applied": applied, "grade": "high"}, "observation_count_heat": 0}
            assert grade_hvac_confidence(cache) == "high"
            coarse = {"cycle_fit_heat": {"applied": applied, "grade": "medium", "ne": 40, "ne_w": 25.0}}
            assert grade_hvac_confidence(coarse) == "medium"  # a whole-degree pool's own cycles never reach high


# ---------------------------------------------------------------------------------------------------------------
# Issue #1512 verifier finding 2: confidence at upgrade (age out, never reset)
# ---------------------------------------------------------------------------------------------------------------

_DAY0 = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)


def _real_shape_engine(tmp_path, now) -> LearningEngine:
    """The verified install's shape at upgrade: heat 5.58 F/h from 65 runs, cool -6.13 from 286, k_p from 386."""
    e = LearningEngine(tmp_path)
    e.load_state()
    e._state.thermal_model_cache = {
        "k_passive": -0.048,
        "k_active_heat": 5.583,
        "k_active_cool": -6.129,
        "observation_count_heat": 65,
        "observation_count_cool": 286,
        "observation_count_passive": 386,
    }
    with patch("custom_components.climate_advisor.learning.dt_util") as dt:
        dt.now.return_value = now
        e._ensure_rate_prior()
    return e


def _first_cycles(e: LearningEngine, now, v3: bool) -> None:
    """The first post-upgrade cycles of the real data: heat none eligible, cool one (a small move it agrees with)."""
    with patch("custom_components.climate_advisor.learning.dt_util") as dt:
        dt.now.return_value = now
        e.apply_cycle_fit("heat", _fit(q=5.62, grade="none", ne=0, er_q=0.0, prior_q=5.62, prior_sd_q=0.61), v3)
        e.apply_cycle_fit(
            "cool", _fit(q=-6.16, grade="low", ne=1, er_q=0.02, prior_q=-6.15, prior_sd_q=0.53, ne_w=1.0), v3
        )


class TestConfidenceAtUpgrade:
    @pytest.mark.parametrize("v3", [True, False])
    def test_no_tier_drop_and_no_decline_without_new_evidence(self, tmp_path, v3):
        """Occupant: a home whose heating/cooling was well learned keeps adaptive thermal behaviour (pre-heat timing,
        the nat-vent thermal-floor veto, the ODE curve) through the upgrade and for as long as no new cycle says
        otherwise. Owner decision 2026-11-11 ("fade only on new evidence"): the verified whole-degree install (no
        eligible cycle, ever) used to go high -> medium at day 87 -> low 108 -> none 129; now it stays high."""
        e = _real_shape_engine(tmp_path, _DAY0)
        assert grade_hvac_confidence({"observation_count_heat": 65, "observation_count_cool": 286}) == "high"
        _first_cycles(e, _DAY0, v3)
        c = e._state.thermal_model_cache
        for d in range(0, 400, 7):
            now = _DAY0 + timedelta(days=d)
            # refits with no new eligible cycle (a whole-degree sensor) change nothing either
            with patch("custom_components.climate_advisor.learning.dt_util") as dt:
                dt.now.return_value = now
                e.apply_cycle_fit("heat", _fit(q=5.62, grade="none", ne=0, er_q=0.0, prior_q=5.62, prior_sd_q=0.61), v3)
            assert grade_hvac_confidence(c, now=now) == "high"
            assert hvac_evidence_count(c, "heat", now) == pytest.approx(65)
            assert hvac_evidence_count(c, "cool", now) == pytest.approx(286 + 1)
        assert c["rate_prior"]["k_active_heat"].get("keep", 1.0) == 1.0

    def test_new_cycles_replace_the_carried_evidence(self, tmp_path):
        e = _real_shape_engine(tmp_path, _DAY0)
        later = _DAY0 + timedelta(days=120)
        with patch("custom_components.climate_advisor.learning.dt_util") as dt:
            dt.now.return_value = later
            e.apply_cycle_fit(
                "cool",
                _fit(q=-6.2, grade="medium", ne=25, er_q=0.7, prior_q=-6.3, prior_sd_q=1.5, ne_w=20.0),
                False,
            )
        assert grade_hvac_confidence(e._state.thermal_model_cache, now=later) == "high"

    def test_a_fit_that_contradicts_the_carried_value_is_graded_on_its_own(self, tmp_path):
        """Data that moved q more than two prior sds away: the old evidence no longer backs the rate."""
        e = _real_shape_engine(tmp_path, _DAY0)
        e._state.thermal_model_cache["rate_prior"].pop("k_active_cool")  # a heat-only home
        e._state.thermal_model_cache["observation_count_cool"] = 0
        with patch("custom_components.climate_advisor.learning.dt_util") as dt:
            dt.now.return_value = _DAY0
            e.apply_cycle_fit("heat", _fit(q=9.0, grade="low", ne=6, prior_q=5.6, prior_sd_q=0.6), True)
        assert grade_hvac_confidence(e._state.thermal_model_cache, now=_DAY0) == "low"


# ---------------------------------------------------------------------------------------------------------------
# Issue #1512 verifier finding 4: a held fit never snaps back to the legacy value
# ---------------------------------------------------------------------------------------------------------------


class TestHeldFit:
    @pytest.mark.parametrize(("mode", "q_new", "legacy"), [("heat", 6.5, 3.0), ("cool", -7.0, -3.5)])
    def test_sensor_swap_holds_the_applied_rate_until_new_cycles_displace_it(self, tmp_path, mode, q_new, legacy):
        """Occupant: swapping the indoor sensor does not make the house's heating/cooling rate jump back to the
        months-old value (pre-heat suddenly far too early or late), and a quiet spell after the swap does not wear
        it down either (owner decision 2026-11-11: it yields only to the new sensor's cycles)."""
        e = _engine(tmp_path)
        field = f"k_active_{mode}"
        with patch("custom_components.climate_advisor.learning.dt_util") as dt:
            dt.now.return_value = _DAY0
            e.apply_cycle_fit(
                mode, _fit(q=q_new, ne=8, ne_w=8.0, entity="sensor.old", t_last=1.0e9, t_eligible=[1.0e9]), True
            )
            assert e._state.thermal_model_cache[field] == pytest.approx(q_new)
            # the next refit sees only the new sensor's cycles: none eligible yet
            e.apply_cycle_fit(mode, _fit(q=legacy, grade="none", ne=0, entity="sensor.new"), True)
            c = e._state.thermal_model_cache
            held = c[f"cycle_fit_{mode}"]["held"]
            assert held["q"] == pytest.approx(q_new) and held["keep"] == 1.0
            assert c[field] == pytest.approx(q_new)  # held, not snapped back to the legacy value
            for days in (0, 21, 89, 200, 400):  # was: half-way to legacy at 21 days, legacy at 200, dropped at ~140
                dt.now.return_value = _DAY0 + timedelta(days=days)
                e._recompute_aged_rates()
                assert c[field] == pytest.approx(q_new, abs=1e-3)
                assert grade_hvac_confidence(c, now=dt.now.return_value) != "none"
            # the new sensor's first agreeing cycles: the held share halves every THERMAL_EVIDENCE_HALF_N of them
            q_b = q_new * 1.02
            e.apply_cycle_fit(
                mode,
                _fit(
                    q=q_b,
                    ne=20,
                    ne_w=15.0,
                    entity="sensor.new",
                    t_last=2.0e9 + 19,
                    t_eligible=[2.0e9 + i for i in range(20)],
                    prior_source="held",
                    prior_q=q_new,
                    prior_sd_q=0.3,
                ),
                True,
            )
            assert c[f"cycle_fit_{mode}"]["held"]["keep"] == pytest.approx(0.5, abs=1e-4)
            assert c[field] == pytest.approx(q_b)
            # 40 more that contradict it (q 50% off, > 2 prior sds): its share halves every 5 of them, falls below
            # THERMAL_EVIDENCE_MIN_KEEP and it is dropped (an evidence rule, not a date)
            e.apply_cycle_fit(
                mode,
                _fit(
                    q=q_new * 1.5,
                    ne=60,
                    ne_w=25.0,
                    entity="sensor.new",
                    t_last=3.0e9 + 39,
                    t_eligible=[2.0e9 + i for i in range(20)] + [3.0e9 + i for i in range(40)],
                    prior_source="held",
                    prior_q=q_new,
                    prior_sd_q=0.3,
                ),
                True,
            )
            assert "held" not in c[f"cycle_fit_{mode}"]

    @pytest.mark.parametrize(("mode", "q_held"), [("heat", 6.5), ("cool", -7.0)])
    def test_held_fit_is_the_prior_of_the_new_sensor_but_never_counted_twice(self, mode, q_held):
        from custom_components.climate_advisor import cycle_model

        held = {"q": q_held, "sd_q": 0.3, "ne_w": 8.0, "entity": "sensor.old", "t_last": 1000.0, "applied": True}
        held["at"] = _DAY0.isoformat()
        cache = {"rate_prior": {}, f"cycle_fit_{mode}": {"ne": 0, "held": held}}
        new_pool = [{"mode": mode, "t_on": 5000.0, "entity": "sensor.new"}]
        pr = cycle_model.prior_from_cache(cache, mode, _DAY0, cycles=new_pool)
        assert pr["source"] == "held" and pr["q"][0] == pytest.approx(q_held, abs=0.05)
        # the old sensor swapped back: its cycles (held's newest among them) are in the pool -> not counted again
        back = [
            {"mode": mode, "t_on": 1000.0, "entity": "sensor.old"},
            {"mode": mode, "t_on": 6000.0, "entity": "sensor.old"},
        ]
        assert cycle_model.prior_from_cache(cache, mode, _DAY0, cycles=back)["source"] == "typical"
        # tools that cannot say which pool the fit sees never use it
        assert cycle_model.prior_from_cache(cache, mode, _DAY0)["source"] == "typical"
        # time alone never ages it out (was: typical after 150 days) ...
        late = cycle_model.prior_from_cache(cache, mode, _DAY0 + timedelta(days=400), cycles=new_pool)
        assert late["source"] == "held" and late["q"] == pr["q"]
        # ... displacement by new cycles does: its sd is inflated by 1/keep, and below the minimum it is unused
        held["keep"] = 0.25
        loose = cycle_model.prior_from_cache(cache, mode, _DAY0, cycles=new_pool)
        assert loose["source"] == "held" and loose["n_eff"] == pytest.approx(8.0 * 0.25)
        held["keep"] = 0.005
        assert cycle_model.prior_from_cache(cache, mode, _DAY0, cycles=new_pool)["source"] == "typical"

    @pytest.mark.parametrize(("mode", "q_old"), [("heat", 6.9), ("cool", -6.9)])
    def test_first_cycle_after_a_long_season_starts_from_the_last_fit(self, tmp_path, mode, q_old):
        """Occupant: the first heating (or cooling) cycles after a summer (winter) without one start from last
        season's learned rate, with no step (verifier: k_active_heat 6.9 -> 5.65 on the first refit after an 89-day
        gap). The store let the old cycles go with the new one (90 days back from the newest), so the previous fit is
        this fit's prior (``held_candidate``) and is kept as held."""
        from custom_components.climate_advisor import cycle_model

        e = _engine(tmp_path)
        with patch("custom_components.climate_advisor.learning.dt_util") as dt:
            dt.now.return_value = _DAY0
            e.apply_cycle_fit(
                mode,
                _fit(
                    q=q_old,
                    sd_q=0.1,
                    ne=40,
                    ne_w=20.0,
                    grade="high",
                    entity="sensor.x",
                    t_last=1.0e9,
                    t_eligible=[1.0e9 - i for i in range(40)],
                    prior_q=q_old * 0.8,
                    prior_sd_q=1.5,
                ),
                True,
            )
            c = e._state.thermal_model_cache
            new_pool = [{"mode": mode, "t_on": 1.0e9 + 120 * 86400, "entity": "sensor.x"}]
            pr = cycle_model.prior_from_cache(c, mode, _DAY0 + timedelta(days=120), cycles=new_pool)
            assert pr["source"] == "held" and pr["q"][0] == pytest.approx(q_old, abs=0.01)
            dt.now.return_value = _DAY0 + timedelta(days=120)
            e.apply_cycle_fit(
                mode,
                _fit(
                    q=q_old * 1.005,
                    ne=1,
                    ne_w=1.0,
                    grade="low",
                    er_q=0.02,
                    entity="sensor.x",
                    t_last=1.0e9 + 120 * 86400,
                    t_eligible=[1.0e9 + 120 * 86400],
                    prior_source="held",
                    prior_q=q_old,
                    prior_sd_q=0.1,
                ),
                True,
            )
            fit = c[f"cycle_fit_{mode}"]
            assert fit["held"]["q"] == pytest.approx(q_old) and fit["held"]["keep"] == pytest.approx(0.5 ** (1 / 20))
            assert c[f"k_active_{mode}"] == pytest.approx(q_old * 1.005)  # no step
            # a small er_q against the strong held prior it agrees with is not low confidence
            assert grade_hvac_confidence({f"cycle_fit_{mode}": fit}) == "high"

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
        c.config = {
            "learning_enabled": True,
            "temp_unit": "fahrenheit",
            "primary_engine": "v3" if v3_primary else "v4",
            **(config or {}),
        }
        c.learning = LearningEngine(tmp_path)
        c.learning.load_state()
        c.learning._state.thermal_model_cache = {"k_passive": -0.05}
        c._cycle_recorder = CycleRecorder(True)
        c._cycle_store = CycleStore(tmp_path / "cycles.json")
        c._cycle_fit_lock = None
        c._cycle_pending = {}
        c._cycle_last_refit = {}
        c._cycle_last_save = None
        c._cycle_companion_entities = ()
        c._cycle_companion_unsub = None
        c._rejection_log = {}
        c._today_record = SimpleNamespace(thermal_session_count=0)
        c._last_outdoor_temp = 40.0
        c.automation_engine = SimpleNamespace(_fan_active=False, _fan_override_active=False, _natural_vent_active=False)
        c._any_sensor_open = lambda: self.windows
        self.windows = False
        c._get_indoor_temp_with_provenance = lambda: SimpleNamespace(primary_value=self.indoor)
        self.primary = "sensor.hall"
        c._primary_indoor_entity = lambda: self.primary
        c._read_thermostat_setpoint_f = lambda state, mode: state.attributes.get("temperature")
        c._zone_log_id = lambda: "z1"
        c._spawn = lambda coro: self.tasks.append(coro)

        async def _job(fn, *args):
            return fn(*args)

        c._executor_job = _job
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
            "_cycle_refit_due_modes",
            "_cycle_save_due",
            "_cycle_spawn_refit",
            "_cycle_prior_snapshot",
            "_cycle_judge",
            "_cycle_companion_wanted",
            "_cycle_sync_companion_listener",
            "_cycle_unsub_companion",
            "_cycle_companion_changed",
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
        for coro in self.tasks:  # undrained work of a test that did not need it
            coro.close()
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

    def test_a_run_that_continues_through_a_setpoint_change_keeps_both_parts(self, rig):
        """Issue #1512 finding 3: the run up to the change and the run after it (chasing the new setpoint) are both
        evidence. The old wiring excluded the whole run as "setpoint_change"."""
        _heat_cycle(rig, setpoint_change_at=10.0, coast=False)
        first, cont = rig.c._cycle_store.cycles("heat")
        assert (first["cut"], first["t_off"]) == ("setpoint_change", None)
        assert cont["t_on"] == first["samples"][-1][0] and cont["cut"] is None and cont["t_off"] is not None
        assert rig.c._rejection_log.get(OBS_TYPE_HVAC_HEAT, []) == []
        assert rig.c._today_record.thermal_session_count == 2  # both judged eligible at once
        rig.at(30 + 46 + 31)  # the throttled refit of the second one runs on the next due tick
        rig.c._cycle_tick()
        rig.drain()
        assert rig.c.learning._state.thermal_model_cache["cycle_fit_heat"]["ne"] == 2

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    @pytest.mark.parametrize("order", ["setpoint_then_action", "action_then_setpoint", "same_event"])
    def test_a_commanded_stop_keeps_the_run(self, rig, mode, order):
        """The normal order of a commanded change (CA's own moves too): the setpoint and the action edge arrive
        seconds apart or in one event. The run is kept up to the action edge; the post-change coast is not used."""
        action = "heating" if mode == "heat" else "cooling"
        sign = 1.0 if mode == "heat" else -1.0
        sp_old, sp_new = (70.0, 66.0) if mode == "heat" else (72.0, 76.0)
        running = _state(mode, action, setpoint=sp_old)
        rig.at(0)
        rig.indoor = 66.0
        rig.thermostat(_state(mode, "idle", setpoint=sp_old), running, False, True)
        for i in range(1, 41):
            rig.at(i * 0.5)
            rig.indoor = round(66.0 + sign * 6.0 * i * 0.5 / 60.0, 2)
            rig.c._cycle_reading()
        rig.at(20)
        if order == "same_event":
            rig.thermostat(running, _state(mode, "idle", setpoint=sp_new), True, False)
        elif order == "setpoint_then_action":
            rig.thermostat(running, _state(mode, action, setpoint=sp_new), True, True)
            rig.at(20 + 20 / 60)
            rig.thermostat(_state(mode, action, setpoint=sp_new), _state(mode, "idle", setpoint=sp_new), True, False)
        else:
            rig.thermostat(running, _state(mode, "idle", setpoint=sp_old), True, False)
            rig.at(20 + 20 / 60)
            rig.thermostat(_state(mode, "idle", setpoint=sp_old), _state(mode, "idle", setpoint=sp_new), False, False)
        for i in range(1, 30):  # the post-change coast
            rig.at(20.5 + i * 0.5)
            rig.indoor = round(rig.indoor + sign * 0.02, 2)
            rig.c._cycle_reading()
        rig.at(70)
        rig.c._cycle_tick()
        rig.drain()
        (cyc,) = rig.c._cycle_store.cycles(mode)
        off = _T0.timestamp() + (20 + (20 / 60 if order == "setpoint_then_action" else 0)) * 60
        assert cyc["cut"] == "setpoint_change" and cyc["t_off"] == pytest.approx(off, abs=1)
        assert cyc["samples"][-1][0] <= off + 21  # nothing after the change is recorded
        fit = rig.c.learning._state.thermal_model_cache[f"cycle_fit_{mode}"]
        assert (
            fit["ne"] == 1
            and rig.c._rejection_log.get(OBS_TYPE_HVAC_HEAT if mode == "heat" else OBS_TYPE_HVAC_COOL) is None
        )

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


class TestCompanionSensor:
    """Issue #1512 finding 1: the readings a cycle is made of come from the entity the band reading uses now."""

    def _rig(self, tmp_path, source, companions=("sensor.companion",)):
        r = _Rig(tmp_path, config={"indoor_temp_source": source, "climate_entity": "climate.t"})
        r.c._companion_temp_sensors = lambda: companions
        r.c._companion_for_resolver = lambda: object()
        r.c._zone_scoped_sync = lambda fn: fn
        r.c.hass = MagicMock()
        return r

    def _open_heat(self, r):
        r.thermostat(_state("heat", "idle"), _state("heat", "heating"), False, True)

    def _samples(self, r):
        return r.c._cycle_recorder._open["heat"]["samples"]

    def test_climate_fallback_with_a_finer_companion_records_the_companion(self, tmp_path):
        """Occupant: a thermostat whose own display is whole degrees but whose finer companion sensor is the band
        reading learns from every companion tick, not only when the coarse thermostat attribute happens to change."""
        r = self._rig(tmp_path, "climate_fallback")
        try:
            r.primary = "sensor.companion"
            with patch(f"{_COORD_MOD}.async_track_state_change_event") as track:
                track.return_value = MagicMock()
                r.c._cycle_sync_companion_listener()
                (args, _kw) = track.call_args
                assert list(args[1]) == ["sensor.companion"]
                r.c._cycle_sync_companion_listener()  # same set: no second subscription
                assert track.call_count == 1
            r.at(0)
            self._open_heat(r)
            for i, v in enumerate((66.1, 66.2, 66.3), start=1):
                r.at(i * 0.5)
                r.indoor = v
                r.c._cycle_companion_changed(SimpleNamespace(data={"entity_id": "sensor.companion"}))
            # the thermostat's own (coarse) attribute change is not a band reading while the companion is in use
            r.at(2)
            r.indoor = 66.35
            r.thermostat(_state("heat", "heating", temp=66.0), _state("heat", "heating", temp=67.0), True, True)
            # another companion candidate the resolver does not use is not recorded either
            r.c._cycle_companion_changed(SimpleNamespace(data={"entity_id": "sensor.other"}))
            assert [s[1] for s in self._samples(r)][1:] == [66.1, 66.2, 66.3]
            # the companion drops out mid-cycle: the thermostat's reading is the band reading again, but one cycle
            # never mixes two sensors' series (the recorder keeps the cycle's first entity)
            r.primary = "climate.t"
            r.at(3)
            r.indoor = 67.0
            r.thermostat(_state("heat", "heating", temp=66.0), _state("heat", "heating", temp=67.0), True, True)
            assert self._samples(r)[-1][1] == 66.3
            r.thermostat(_state("heat", "heating"), _state("heat", "idle"), True, False)
            r.at(60)
            r.c._cycle_tick()
            self._open_heat(r)  # the next cycle is the thermostat's
            r.at(61)
            r.indoor = 67.5
            r.thermostat(_state("heat", "heating", temp=67.0), _state("heat", "heating", temp=68.0), True, True)
            assert self._samples(r)[-1][1] == 67.5
            r.c._cycle_unsub_companion()
            assert r.c._cycle_companion_entities == ()
        finally:
            r.close()

    @pytest.mark.parametrize("source", ["sensor", "input_number"])
    def test_sensor_and_input_number_sources_record_their_own_listener_only(self, tmp_path, source):
        r = self._rig(tmp_path, source)
        try:
            assert r.c._cycle_companion_wanted() == ()  # no companion subscription for a dedicated sensor
            r.at(0)
            self._open_heat(r)
            r.at(0.5)
            r.indoor = 66.4
            r.c._cycle_reading()  # the indoor-entity listener
            r.at(1)
            r.indoor = 66.6
            r.thermostat(_state("heat", "heating", temp=66.0), _state("heat", "heating", temp=67.0), True, True)
            assert [s[1] for s in self._samples(r)][1:] == [66.4]
        finally:
            r.close()

    def test_climate_only_records_the_thermostat_attribute(self, tmp_path):
        r = self._rig(tmp_path, "climate_fallback", companions=())
        try:
            r.primary = "climate.t"
            assert r.c._cycle_companion_wanted() == ()
            r.at(0)
            self._open_heat(r)
            r.at(1)
            r.indoor = 67.0
            r.thermostat(_state("heat", "heating", temp=66.0), _state("heat", "heating", temp=67.0), True, True)
            assert self._samples(r)[-1][1] == 67.0
        finally:
            r.close()


class TestThrottleAndSave:
    def test_refits_are_throttled_and_the_file_save_is_debounced(self, rig):
        """Finding 5/7: one cycle file write per 30 minutes (plus HA stop), a full refit at most every 30 minutes
        once a mode has learned something (sooner after 3 new eligible cycles); every cycle is still judged at once."""
        calls = []
        orig = rig.c._cycle_refit

        async def _counting(mode, now_ts):
            calls.append(mode)
            await orig(mode, now_ts)

        rig.c._cycle_refit = _counting
        saves = []
        real_save = rig.c._cycle_store.save
        rig.c._cycle_store.save = lambda: saves.append(1) or real_save()
        _heat_cycle(rig, start_min=0.0)  # first eligible cycle: refit at once, first save
        assert calls == ["heat"] and saves == [1]
        _heat_cycle(rig, start_min=80.0)  # 2nd: within 30 min of the last refit? no - 80 min later: refit
        _heat_cycle(rig, start_min=100.0, run_min=10.0)
        n_after = len(calls)
        assert len(rig.c._cycle_store.cycles("heat")) == 3
        assert rig.c._today_record.thermal_session_count == 3  # judged at once regardless of the throttle
        assert n_after <= 3 and len(saves) <= n_after
        # a waiting cycle is refitted by the 5-minute tick once due, and HA stop saves what is still dirty
        rig.at(400)
        rig.c._cycle_tick()
        rig.drain()
        assert rig.c._cycle_pending == {}
        asyncio.run(rig.c._persist_cycles())
        assert not rig.c._cycle_store.dirty

    def test_startup_refits_each_mode_with_stored_cycles(self, rig):
        _heat_cycle(rig)
        rig.c.learning._state.thermal_model_cache.pop("cycle_fit_heat")
        rig.c._cycle_last_refit.clear()
        rig.c._cycle_spawn_refit(startup=True)
        rig.drain()
        c = rig.c.learning._state.thermal_model_cache
        assert c["cycle_fit_heat"]["ne"] == 1 and "cycle_fit_cool" not in c


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
    @pytest.mark.parametrize(("engine", "expected"), [("v3", True), ("v4", False), ("bogus", False)])
    def test_primary_is_read_from_the_engine_setting(self, tmp_path, engine, expected):
        """Finding 8: read as decision_rates_summary does (prediction_engines.primary_id). The old test required an
        identity probe of control_thermal_model, an undocumented detail that a harmless refactor of the control feed
        (returning a copy) would silently turn into "never v3" and stop the v3 decisions from learning."""
        r = _Rig(tmp_path, config={"primary_engine": engine})
        try:
            # an unknown value reads as the default engine (v4), exactly like the rest of the integration
            assert r.c._cycle_primary_is_v3() is expected
        finally:
            r.close()

    def test_retired_coordinator_symbols_are_gone(self):
        for name in (
            "_closed_house_rate",
            "_reject_hvac_run",
            "_run_duty_balance_chart_log_fit",
            "_maybe_run_periodic_duty_balance_fit",
        ):
            assert not hasattr(Coord, name), name
        assert not hasattr(_coord_mod, "hvac_rate_fit")


# ---------------------------------------------------------------------------------------------------------------
# Owner decision 2026-11-11 ("Fade only on new evidence"): displacement by new cycles, never by time
# ---------------------------------------------------------------------------------------------------------------


def _new_fit(mode: str, q: float, t0: float, n_new: int, *, prior_q: float, prior_sd_q: float = 0.6, **kw):
    sign = 1.0 if mode == "heat" else -1.0
    t_el = [t0 + i * 3600.0 for i in range(n_new)]
    return _fit(
        q=sign * q,
        ne=n_new,
        ne_w=float(n_new),
        grade="medium",
        entity="sensor.x",
        t_last=t_el[-1],
        t_eligible=t_el,
        prior_q=sign * prior_q,
        prior_sd_q=prior_sd_q,
        prior_source="cache",
        **kw,
    )


class TestEvidenceDisplacement:
    @pytest.mark.parametrize("v3", [True, False])
    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_agreeing_cycles_displace_slowly_contradicting_ones_fast(self, tmp_path, mode, v3):
        """Occupant: the old (pre-upgrade) rate gives way to the house's own measured cycles: gently while they
        agree with it, fast when they say it was wrong. Same rule for heat and cool, v3 and v4."""
        e = _real_shape_engine(tmp_path, _DAY0)
        field = f"k_active_{mode}"
        n = 65 if mode == "heat" else 286
        with patch("custom_components.climate_advisor.learning.dt_util") as dt:
            dt.now.return_value = _DAY0
            e.apply_cycle_fit(mode, _new_fit(mode, 5.7, 1.0e9, 10, prior_q=5.6), v3)  # agrees: 10 / 20
            rec = e._state.thermal_model_cache["rate_prior"][field]
            assert rec["keep"] == pytest.approx(0.5**0.5, abs=1e-5)
            assert hvac_evidence_count(e._state.thermal_model_cache, mode) == pytest.approx(n * 0.5**0.5 + 10, abs=0.01)
            e.apply_cycle_fit(mode, _new_fit(mode, 9.0, 2.0e9, 10, prior_q=5.6), v3)  # contradicts: 10 / 5
            assert rec["keep"] == pytest.approx(0.5**0.5 * 0.25, abs=1e-5)

    @pytest.mark.parametrize("mode", ["heat", "cool"])
    def test_the_same_cycles_never_count_twice_restart_and_crash(self, tmp_path, mode):
        """Startup refits the same cycles; a crash may lose the newest cycles (the file is saved periodically) while
        learning.json already counted them. Neither displaces the old evidence again; the next new cycle counts
        once."""
        e = _real_shape_engine(tmp_path, _DAY0)
        field = f"k_active_{mode}"
        with patch("custom_components.climate_advisor.learning.dt_util") as dt:
            dt.now.return_value = _DAY0
            fit = _new_fit(mode, 5.7, 1.0e9, 10, prior_q=5.6)
            e.apply_cycle_fit(mode, fit, True)
            keep = e._state.thermal_model_cache["rate_prior"][field]["keep"]
            e.apply_cycle_fit(mode, dict(fit), True)  # restart: the same pool again
            lost = _new_fit(mode, 5.7, 1.0e9, 7, prior_q=5.6)  # crash: the newest three cycles never reached disk
            e.apply_cycle_fit(mode, lost, True)
            assert e._state.thermal_model_cache["rate_prior"][field]["keep"] == keep
            assert e._state.thermal_model_cache[f"cycle_fit_{mode}"]["t_seen"] == fit["t_last"]
            nxt = _new_fit(mode, 5.7, 1.0e9, 7, prior_q=5.6)
            nxt["t_eligible"] = [*nxt["t_eligible"], 1.0e9 + 50 * 3600.0]
            nxt["t_last"] = nxt["t_eligible"][-1]
            e.apply_cycle_fit(mode, nxt, True)
            assert e._state.thermal_model_cache["rate_prior"][field]["keep"] == pytest.approx(keep * 0.5 ** (1 / 20))

    def test_startup_with_a_partial_cycle_file_recovers_without_an_error(self, tmp_path):
        """A crash mid-write cannot leave a partial file (atomic replace), but a damaged one loads as far as it can
        and the refit at startup runs on what is there."""
        r = _Rig(tmp_path)
        try:
            _heat_cycle(r)
            asyncio.run(r.c._persist_cycles())
            path = tmp_path / "cycles.json"
            path.write_text(path.read_text()[:-40])  # truncated
            r.c._cycle_store.load()
            assert len(r.c._cycle_store) == 0
            r.c._cycle_spawn_refit(startup=True)
            r.drain()
            assert r.c.learning._state.thermal_model_cache["cycle_fit_heat"]["ne"] == 1  # the earlier fit stands
        finally:
            r.close()


def _hc(action: str, low: float = 68.0, high: float = 76.0, temp: float = 66.0, mode: str = "heat_cool"):
    return SimpleNamespace(
        state=mode,
        attributes={
            "hvac_action": action,
            "target_temp_low": low,
            "target_temp_high": high,
            "current_temperature": temp,
        },
    )


class TestHeatCoolSetpointGuard:
    @pytest.mark.parametrize(
        ("mode", "action", "other", "own"),
        [
            ("heat", "heating", "target_temp_high", "target_temp_low"),
            ("cool", "cooling", "target_temp_low", "target_temp_high"),
        ],
    )
    def test_only_the_running_modes_setpoint_cuts_the_run(self, rig, mode, action, other, own):
        """Occupant on an auto (heat_cool) thermostat: nudging the cooling target while the furnace runs (or the
        heating target while the AC runs) no longer cuts that run out of the learning; changing the target of the
        running mode still does (Issue #1512 finding 3 handling)."""
        rig.c._read_thermostat_setpoint_f = types.MethodType(Coord._read_thermostat_setpoint_f, rig.c)
        rig.at(0)
        rig.thermostat(_hc("idle"), _hc(action), False, True)
        assert rig.c._cycle_recorder.is_running(mode)
        rig.at(5)
        moved = {other: (78.0 if other == "target_temp_high" else 64.0)}
        rig.thermostat(_hc(action), _hc(action, **{k.split("_")[-1]: v for k, v in moved.items()}), True, True)
        assert rig.c._cycle_recorder._open[mode].get("_sp") is None  # not cut
        rig.at(6)
        own_moved = {own.split("_")[-1]: (70.0 if own == "target_temp_low" else 74.0)}
        base = {k.split("_")[-1]: v for k, v in moved.items()}
        rig.thermostat(_hc(action, **base), _hc(action, **{**base, **own_moved}), True, True)
        assert rig.c._cycle_recorder._open[mode].get("_sp") is not None  # the governing setpoint cuts it
