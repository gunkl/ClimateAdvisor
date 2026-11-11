"""Issue #1524: the primary solar phase offset gives way only to newer AC-duty evidence, never to the calendar.

Occupant: the solar phase offset sets the hour the forecast expects the sun's heat to reach the rooms (pre-cool
timing, the predicted-indoor curve). It used to switch from the passive-window value to the AC-duty value on the 91st
day after the last passive-window measurement, with no new measurement arriving: the expected peak hour could move by
an hour or more overnight. Now the AC-duty days displace the primary as they arrive: gradually when they agree with it,
faster when they contradict it, and a new passive-window measurement restores it.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from custom_components.climate_advisor.const import (
    THERMAL_EVIDENCE_HALF_N,
    THERMAL_EVIDENCE_HALF_N_CONTRADICT,
)
from custom_components.climate_advisor.learning import (
    LearningEngine,
    _resolve_solar_phase_offset,
    migrate_solar_phase_keep,
)

PRIMARY = 3.0


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _engine(tmp_path) -> LearningEngine:
    e = LearningEngine(tmp_path)
    e.load_state()
    assert e.update_solar_phase_offset(PRIMARY, 0.1, window_end="2026-04-01T15:00:00+00:00")
    return e


def _ac_days(e: LearningEngine, offset: float, n: int, first: date = date(2026, 6, 1)) -> None:
    for i in range(n):
        assert e.update_ac_duty_solar_phase_offset(offset, (first + timedelta(days=i)).isoformat())


def _resolved(e: LearningEngine) -> float:
    return _resolve_solar_phase_offset(e._state.thermal_model_cache)


class TestNoCalendarSwitch:
    def test_quiet_primary_for_120_days_does_not_switch_on_day_91(self, tmp_path):
        """The primary's last passive window is 90, then 91, then 120 days old; the secondary is fresh and
        qualified the whole time, but no NEW AC-duty day arrives: the offset in use must not move."""
        e = _engine(tmp_path)
        _ac_days(e, 1.5, 5)  # five contradicting AC-duty days, counted once, when they arrived
        c = e._state.thermal_model_cache
        seen = []
        for age in (60, 90, 91, 120):
            c["solar_phase_offset_last_obs_date"] = _days_ago(age)
            c["solar_phase_offset_ac_last_obs_date"] = _days_ago(age - 80)
            seen.append(_resolved(e))
        # old behaviour: 3.0 at 60 and 90 days, then 1.5 from day 91 (a 1.5 h jump with nothing new measured)
        assert seen == [pytest.approx(seen[0])] * 4
        assert 1.5 < seen[0] < PRIMARY  # the five days did displace part of it, by evidence


@pytest.mark.parametrize(
    ("ac_offset", "half"),
    [(2.5, THERMAL_EVIDENCE_HALF_N), (1.0, THERMAL_EVIDENCE_HALF_N_CONTRADICT)],
    ids=["agreeing", "contradicting"],
)
class TestEvidenceSwitch:
    def test_newer_secondary_days_move_it_gradually(self, tmp_path, ac_offset, half):
        e = _engine(tmp_path)
        _ac_days(e, ac_offset, 3)  # the secondary qualifies (THERMAL_SOLAR_PHASE_AC_MIN_OBS)
        gap = PRIMARY - ac_offset
        # three days are a small share, not a switch (old behaviour with a fresh primary: no move at all, ever)
        assert _resolved(e) == pytest.approx(PRIMARY - gap * (1 - 0.5 ** (3 / half)))
        assert PRIMARY - _resolved(e) < 0.5 * gap
        _ac_days(e, ac_offset, int(half) - 3, first=date(2026, 7, 1))
        assert _resolved(e) == pytest.approx(PRIMARY - gap / 2)  # halfway after `half` new days
        _ac_days(e, ac_offset, 7 * int(half), first=date(2026, 8, 1))
        assert _resolved(e) == pytest.approx(ac_offset, abs=0.01 * gap)

    def test_a_new_passive_window_restores_the_primary(self, tmp_path, ac_offset, half):
        e = _engine(tmp_path)
        _ac_days(e, ac_offset, int(half))
        assert e.update_solar_phase_offset(PRIMARY, 0.1, window_end="2026-09-01T15:00:00+00:00")
        assert _resolved(e) == pytest.approx(PRIMARY)


def test_contradicting_days_switch_faster_than_agreeing_ones(tmp_path):
    agree, contra = _engine(tmp_path / "a"), _engine(tmp_path / "c")
    _ac_days(agree, 2.5, 5)
    _ac_days(contra, 1.0, 5)
    share = {
        k: (PRIMARY - _resolved(e)) / (PRIMARY - off) for k, e, off in (("agree", agree, 2.5), ("contra", contra, 1.0))
    }
    assert share["contra"] == pytest.approx(0.5, abs=1e-5)  # 5 contradicting days halve the primary's share
    assert share["agree"] == pytest.approx(1 - 0.5 ** (5 / THERMAL_EVIDENCE_HALF_N), abs=1e-5)  # ~0.16


class TestCountedOnce:
    def test_an_ac_day_is_counted_once(self, tmp_path):
        """The estimator re-scans the whole chart log daily; a day already counted is not new evidence."""
        e = _engine(tmp_path)
        _ac_days(e, 1.0, 5)
        c = dict(e._state.thermal_model_cache)
        for _ in range(3):  # three more daily re-scans of the same five days
            for i in range(5):
                assert not e.update_ac_duty_solar_phase_offset(1.0, (date(2026, 6, 1) + timedelta(days=i)).isoformat())
        after = e._state.thermal_model_cache
        assert after["solar_phase_offset_ac_obs_count"] == c["solar_phase_offset_ac_obs_count"] == 5
        assert after["solar_phase_offset_keep"] == c["solar_phase_offset_keep"]

    def test_a_passive_window_is_counted_once(self, tmp_path):
        e = _engine(tmp_path)
        _ac_days(e, 1.0, 5)
        keep = e._state.thermal_model_cache["solar_phase_offset_keep"]
        # the daily re-fit scans two days: the same window again neither re-weighs nor restores the primary
        assert not e.update_solar_phase_offset(4.0, 0.1, window_end="2026-04-01T15:00:00+00:00")
        assert e._state.thermal_model_cache["solar_phase_offset_h"] == PRIMARY
        assert e._state.thermal_model_cache["solar_phase_offset_keep"] == keep


class TestUpgradeContinuity:
    def _old(self, primary_age: int, secondary_age: int) -> dict:
        return {
            "solar_phase_offset_h": PRIMARY,
            "solar_phase_offset_last_obs_date": _days_ago(primary_age),
            "solar_phase_offset_ac_h": 1.5,
            "solar_phase_offset_ac_last_obs_date": _days_ago(secondary_age),
            "solar_phase_offset_ac_obs_count": 12,
        }

    @pytest.mark.parametrize(
        ("primary_age", "secondary_age", "in_use"),
        [(91, 1, 1.5), (10, 1, PRIMARY), (91, 91, PRIMARY)],
        ids=["old-rule-used-secondary", "fresh-primary", "both-quiet"],
    )
    def test_the_offset_in_use_does_not_change_at_the_upgrade(self, tmp_path, primary_age, secondary_age, in_use):
        cache = self._old(primary_age, secondary_age)
        assert migrate_solar_phase_keep(cache, date.today())
        assert _resolve_solar_phase_offset(cache) == pytest.approx(in_use)
        assert not migrate_solar_phase_keep(cache, date.today() + timedelta(days=400))  # set once, never again

    def test_load_sets_the_share(self, tmp_path):
        e = LearningEngine(tmp_path)
        e.load_state()
        e._state.thermal_model_cache = self._old(91, 1)
        e.save_state()
        again = LearningEngine(tmp_path)
        again.load_state()
        assert again._state.thermal_model_cache["solar_phase_offset_keep"] == 0.0
        assert _resolve_solar_phase_offset(again._state.thermal_model_cache) == pytest.approx(1.5)
