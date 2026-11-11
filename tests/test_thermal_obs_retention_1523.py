"""Issue #1523: a kind of thermal observation that goes quiet for a season keeps its observations and its rate.

Occupant: after a summer with the windows open (vent fits only, no closed-house fit), the closed-house leak rate the
decisions use (setback depth, pre-heat timing, the nat-vent thermal-floor check) used to fall back to its pre-upgrade
value, because every new observation of any kind trimmed the list to 90 days before today and to 200 overall. And when
the closed-house fits came back in autumn, the first one replaced the summer-old value in one step, because the old
ones weighed 0.5 ** (age_days / 21). Owner decision 2026-11-11 ("fade only on new evidence"): now each kind keeps its
own share of the cap (its own oldest leaves first, nothing leaves with time), and an observation's weight halves per
THERMAL_EVIDENCE_HALF_N newer observations of its rate, not per 21 days.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from custom_components.climate_advisor.const import (
    THERMAL_EVIDENCE_HALF_N,
    THERMAL_OBS_CAP,
    THERMAL_OBS_KIND_CAPS,
)
from custom_components.climate_advisor.learning import (
    LearningEngine,
    thermal_obs_kind,
    trim_thermal_observations,
)
from custom_components.climate_advisor.thermal_confidence import grade_passive_confidence, passive_evidence_count

T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)
PRE_UPGRADE_K = -0.050  # the closed-house rate cached before the upgrade (the value the bug reverted to)
SPRING_K = -0.040  # what the spring's closed-house fits learned


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def at(self, day: float, minute: int = 0) -> datetime:
        self.now = T0 + timedelta(days=day, minutes=minute)
        return self.now


@contextmanager
def _engine(tmp_path, cache: dict | None = None):
    clock = _Clock()
    with patch("custom_components.climate_advisor.learning.dt_util.now", lambda *a, **k: clock.now):
        e = LearningEngine(tmp_path)
        e.load_state()
        if cache is not None:
            e._state.thermal_model_cache = dict(cache)
            e._ensure_rate_prior()
        yield e, clock


def _record(e: LearningEngine, clock: _Clock, mode: str, day: float, minute: int = 0, **fields) -> None:
    now = clock.at(day, minute)
    obs = {
        "hvac_mode": mode,
        "date": now.date().isoformat(),
        "timestamp": now.isoformat(),
        "confidence_grade": "medium",
        **fields,
    }
    assert e.record_thermal_observation(obs)


def _count(e: LearningEngine, kind: str) -> int:
    return sum(1 for o in e._state.thermal_observations if thermal_obs_kind(o) == kind)


def _spring(e: LearningEngine, clock: _Clock) -> None:
    """90 days: two closed-house fits a day around SPRING_K and one windows-open fit."""
    for d in range(90):
        _record(e, clock, "passive", d, 0, k_passive=SPRING_K + (0.004 if d % 2 else -0.004))
        _record(e, clock, "passive", d, 1, k_passive=SPRING_K)
        _record(e, clock, "vent_window", d, 2, k_passive=-0.30)


_UPGRADED = {
    "k_passive": PRE_UPGRADE_K,
    "observation_count_passive": 30,
    "observation_count_heat": 20,
    "observation_count_cool": 20,
}


class TestQuietPassiveSeason:
    def test_k_passive_and_confidence_hold_through_120_days_of_vent_only(self, tmp_path):
        """Windows open all summer: 120 days, three vent fits a day, no closed-house fit."""
        with _engine(tmp_path, _UPGRADED) as (e, clock):
            _spring(e, clock)
            c = e._state.thermal_model_cache
            before = (c["k_passive"], grade_passive_confidence(c), passive_evidence_count(c), _count(e, "passive"))
            assert before[0] == pytest.approx(SPRING_K)
            seen = []
            for d in range(90, 210):
                for i in range(3):
                    _record(e, clock, "vent_window", d, i, k_passive=-0.30)
                seen.append(
                    (c["k_passive"], grade_passive_confidence(c), passive_evidence_count(c), _count(e, "passive"))
                )
            # old behaviour: k_passive -0.040 -> -0.050 (the pre-upgrade value) by day 179, passive kept 133 -> 0
            assert set(seen) == {before}

    def test_resume_after_the_gap_has_no_step(self, tmp_path):
        """Autumn: the closed-house fits come back at a different value; the rate moves over many fits, not one."""
        with _engine(tmp_path, _UPGRADED) as (e, clock):
            _spring(e, clock)
            for d in range(90, 210):
                _record(e, clock, "vent_window", d, 0, k_passive=-0.30)
            c = e._state.thermal_model_cache
            path = []
            for j in range(2 * int(THERMAL_EVIDENCE_HALF_N)):
                _record(e, clock, "passive", 210 + j, 0, k_passive=-0.030)
                path.append(c["k_passive"])
            # old behaviour: the first autumn fit took over at once (-0.050 -> -0.030 in one step)
            assert path[:5] == [pytest.approx(SPRING_K)] * 5
            # each fit moves it at most to the next value the kept fits hold (spring spread is +-0.004)
            steps = [abs(b - a) for a, b in zip([SPRING_K, *path], path, strict=False)]
            assert max(steps) <= 0.006 + 1e-9
            assert path[-1] == pytest.approx(-0.030)  # new evidence does take over

    def test_time_alone_prunes_nothing(self, tmp_path):
        """A closed-house fit a year older than everything else stays while its kind is under its share."""
        with _engine(tmp_path) as (e, clock):
            _record(e, clock, "passive", 0, k_passive=-0.04)
            for d in range(400, 420):
                _record(e, clock, "solar", d, k_solar=1.0)
            _record(e, clock, "passive", 420, k_passive=-0.04)
            assert _count(e, "passive") == 2  # old behaviour: 1 (the day-0 fit was trimmed at 90 days)


@pytest.mark.parametrize("mode", ["heat", "cool"])
class TestQuietHvacSeasonMirror:
    def test_aged_k_active_holds_through_the_other_season(self, tmp_path, mode):
        """Mirror: on a zone where no cycle fit is applied (v4), k_active_<mode> is the aged value of the stored
        run-edge observations. A season of the other mode (and of passive/vent/solar fits) must not evict them; it
        used to revert k_active to the pre-upgrade value after 90 days."""
        field = f"k_active_{mode}"
        pre, learned = (4.0, 6.0) if mode == "heat" else (-4.0, -6.0)
        with _engine(tmp_path, {field: pre, f"observation_count_{mode}": 4}) as (e, clock):
            for d in range(0, 30, 4):  # 8 stored run-edge runs (the heat/cool share is 10)
                _record(e, clock, mode, d, k_active=learned, method="run_edge")
            c = e._state.thermal_model_cache
            assert c[field] == pytest.approx(learned)
            for d in range(30, 180):
                _record(e, clock, "passive", d, 0, k_passive=-0.04)
                _record(e, clock, "vent_fan", d, 1, k_passive=-0.4)
                _record(e, clock, "solar", d, 2, k_solar=1.0)
                assert c[field] == pytest.approx(learned), f"day {d}"
            assert _count(e, mode) == 8  # old behaviour: 0 by day 119 (k_active back to the pre-upgrade value)


class TestCapSharedPerKind:
    def test_shares_sum_to_the_total(self):
        assert sum(THERMAL_OBS_KIND_CAPS.values()) == THERMAL_OBS_CAP

    @pytest.mark.parametrize("flood", ["vent_window", "vent_fan", "solar"])
    def test_a_flood_of_one_kind_evicts_only_its_own(self, tmp_path, flood):
        with _engine(tmp_path, _UPGRADED) as (e, clock):
            for d in range(60):
                _record(e, clock, "passive", d, k_passive=SPRING_K)
            k_before = e._state.thermal_model_cache["k_passive"]
            fields = {"k_solar": 1.0} if flood == "solar" else {"k_passive": -0.3}
            for i in range(500):
                _record(e, clock, flood, 60 + i / 10, **fields)
            assert _count(e, "passive") == 60  # old behaviour: 0 (the shared 200 were all vent fits)
            assert _count(e, flood) == THERMAL_OBS_KIND_CAPS[flood]
            assert len(e._state.thermal_observations) <= THERMAL_OBS_CAP
            assert e._state.thermal_model_cache["k_passive"] == k_before
            # the kind keeps its newest
            dates = [o["timestamp"] for o in e._state.thermal_observations if o["hvac_mode"] == flood]
            assert dates == sorted(dates) and dates[-1] == clock.now.isoformat()

    def test_a_kind_full_of_its_own_drops_its_oldest(self, tmp_path):
        with _engine(tmp_path) as (e, clock):
            cap = THERMAL_OBS_KIND_CAPS["passive"]
            for i in range(cap + 5):
                _record(e, clock, "passive", i, k_passive=-0.04, window_start=f"w{i}")
            kept = [o["window_start"] for o in e._state.thermal_observations]
            assert kept == [f"w{i}" for i in range(5, cap + 5)]

    def test_a_kind_stored_above_its_share_is_never_cut_at_once(self, tmp_path):
        """A list stored before the shares (150 passive fits) keeps 150: one in, one out, so k_passive cannot step."""
        with _engine(tmp_path) as (e, clock):
            e._state.thermal_observations = [
                {"hvac_mode": "passive", "k_passive": -0.04, "date": "2025-12-01", "n": i} for i in range(150)
            ]
            _record(e, clock, "passive", 0, k_passive=-0.03)
            assert _count(e, "passive") == 150
            assert e._state.thermal_observations[0]["n"] == 1  # only the oldest left
            assert e._state.thermal_model_cache["k_passive"] == pytest.approx(-0.04)

    def test_load_and_the_next_commit_prune_nothing(self, tmp_path):
        """Observations two years old survive a reload and the next commit of another kind (old behaviour: that
        commit trimmed everything dated more than 90 days before today, so 151 -> 1)."""
        with _engine(tmp_path) as (e, clock):
            e._state.thermal_observations = [
                {"hvac_mode": "passive", "k_passive": -0.04, "date": "2024-01-01", "n": i} for i in range(150)
            ] + [{"hvac_mode": "heat", "k_active": 5.0, "date": "2024-01-01", "method": "run_edge"}]
            e.save_state()
            again = LearningEngine(tmp_path)
            again.load_state()
            assert len(again._state.thermal_observations) == 151
            _record(again, clock, "vent_fan", 0, k_passive=-0.3)
            assert len(again._state.thermal_observations) == 152

    def test_trim_leaves_other_kinds_untouched(self):
        obs = [{"hvac_mode": "passive", "n": i} for i in range(3)] + [
            {"hvac_mode": "vent_fan", "n": i} for i in range(40)
        ]
        out = trim_thermal_observations(obs, "vent_fan")
        assert [o["n"] for o in out if o["hvac_mode"] == "passive"] == [0, 1, 2]
        assert [o["n"] for o in out if o["hvac_mode"] == "vent_fan"] == list(range(10, 40))

    def test_unknown_mode_shares_the_other_bucket(self):
        assert thermal_obs_kind({"hvac_mode": "fan_only"}) == "other"
        assert thermal_obs_kind({}) == "other"
        assert thermal_obs_kind({"hvac_mode": "passive"}) == "passive"
