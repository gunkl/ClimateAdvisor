"""Promise misses count over the last 30 days, not for ever (Issue #1490).

Occupant first: the "Promises missed" tile used to count every miss since install, so one bad week
months ago stayed in the number for good and the owner could not tell whether the house is fine
now. Misses (and excused time) now live in per-day buckets, keyed by local date, and a bucket ages
out 30 days after its day. Lifetime totals from before the change are discarded at upgrade.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import patch

from custom_components.climate_advisor.invariant_watchdog import PROMISES
from custom_components.climate_advisor.promise_journal import (
    PROMISE_STATS_WINDOW_DAYS,
    bump_promise_excused,
    bump_promise_stats,
    prune_promise_stats,
    sanitize_promise_stats,
)

ALLOWED = frozenset(p.key for p in PROMISES)
DAY0 = datetime(2026, 9, 1, 21, 30, tzinfo=UTC)


def day(n: int, hour: int = 9) -> datetime:
    return (DAY0 + timedelta(days=n)).replace(hour=hour, minute=0)


def test_the_window_is_thirty_days():
    assert PROMISE_STATS_WINDOW_DAYS == 30


class TestMissesAgeOut:
    def test_a_miss_on_day_0_is_counted_through_day_30_and_gone_on_day_31(self):
        stats: dict = {}
        bump_promise_stats(stats, "settling_flip", DAY0, DAY0)
        assert stats["settling_flip"]["hits"] == 1
        assert sanitize_promise_stats(stats, ALLOWED, day(30, 23))["settling_flip"]["hits"] == 1
        assert sanitize_promise_stats(stats, ALLOWED, day(31, 0)) == {}  # nothing left: the entry goes too

    def test_only_the_old_day_drops_off(self):
        stats: dict = {}
        bump_promise_stats(stats, "settling_flip", DAY0, DAY0)
        bump_promise_stats(stats, "settling_flip", day(20), day(20))
        bump_promise_stats(stats, "settling_flip", day(20, 10), day(20, 10))
        prune_promise_stats(stats, day(31))
        entry = stats["settling_flip"]
        assert entry["hits"] == 2
        assert list(entry["days"]) == [day(20).date().isoformat()]
        assert entry["last_ts"] == day(20, 10).isoformat()

    def test_a_bump_prunes_old_days(self):
        stats: dict = {}
        bump_promise_stats(stats, "settling_flip", DAY0, DAY0)
        bump_promise_stats(stats, "settling_flip", day(40), day(40))
        assert stats["settling_flip"]["hits"] == 1
        assert list(stats["settling_flip"]["days"]) == [day(40).date().isoformat()]

    def test_the_bucket_is_the_local_date_of_the_miss(self):
        """A miss at 21:30 UTC is the next morning in UTC+9 and the same evening in UTC-8."""
        for offset, expected in ((9, "2026-09-02"), (-8, "2026-09-01")):
            tz = timezone(timedelta(hours=offset))
            stats: dict = {}
            bump_promise_stats(stats, "settling_flip", DAY0, DAY0.astimezone(tz))
            assert list(stats["settling_flip"]["days"]) == [expected]

    def test_last_ts_older_than_the_window_is_dropped(self):
        raw = {"settling_flip": {"days": {day(25).date().isoformat(): 1}, "last_ts": DAY0.isoformat()}}
        out = sanitize_promise_stats(raw, ALLOWED, day(31))
        assert out["settling_flip"]["hits"] == 1
        assert out["settling_flip"]["last_ts"] is None


class TestRestart:
    def test_counts_survive_a_save_and_restore(self):
        stats: dict = {}
        bump_promise_stats(stats, "settling_flip", day(1), day(1))
        bump_promise_stats(stats, "settling_flip", day(3), day(3))
        bump_promise_excused(stats, "comfort_kept", "thermostat_deadband", day(2), 15.0, new_run=True, now=day(2))
        restored = sanitize_promise_stats(json.loads(json.dumps(stats)), ALLOWED, day(4))
        assert restored == stats
        assert restored["settling_flip"]["hits"] == 2

    def test_old_lifetime_totals_are_discarded_without_error(self):
        """Pre-#1490 data: {hits, first_ts, last_ts, excused{runs, minutes}} with no day buckets."""
        old = {
            "settling_flip": {"hits": 40, "first_ts": "2026-07-01T00:00:00+00:00", "last_ts": day(1).isoformat()},
            "comfort_kept": {
                "hits": 0,
                "first_ts": None,
                "last_ts": None,
                "excused": {"thermostat_deadband": {"runs": 9, "minutes": 600.0, "last_ts": day(1).isoformat()}},
            },
        }
        assert sanitize_promise_stats(old, ALLOWED, day(2)) == {}

    def test_garbage_buckets_are_dropped(self):
        raw = {
            "settling_flip": {
                "days": {
                    day(1).date().isoformat(): 2,
                    "not-a-date": 1,
                    day(2).date().isoformat(): True,
                    day(3).date().isoformat(): -1,
                    day(4).date().isoformat(): "3",
                    day(90).date().isoformat(): 1,  # far future: clock trouble, never ages out sensibly
                },
                "hits": 999,  # derived, never trusted from disk
                "last_ts": 5,
            }
        }
        out = sanitize_promise_stats(raw, ALLOWED, day(5))
        assert out == {"settling_flip": {"days": {day(1).date().isoformat(): 2}, "hits": 2, "last_ts": None}}

    def test_unknown_promises_and_non_dicts_are_dropped(self):
        assert sanitize_promise_stats(["x"], ALLOWED, day(1)) == {}
        assert sanitize_promise_stats({"evil": {"days": {day(1).date().isoformat(): 1}}}, ALLOWED, day(1)) == {}


class TestExcusedAgesOut:
    def test_excused_runs_and_minutes_are_thirty_day_sums(self):
        stats: dict = {}
        bump_promise_excused(stats, "comfort_kept", "thermostat_deadband", DAY0, 10.0, new_run=True, now=DAY0)
        bump_promise_excused(stats, "comfort_kept", "thermostat_deadband", day(10), 20.0, new_run=True, now=day(10))
        row = stats["comfort_kept"]["excused"]["thermostat_deadband"]
        assert (row["runs"], row["minutes"]) == (2, 30.0)
        prune_promise_stats(stats, day(31))
        row = stats["comfort_kept"]["excused"]["thermostat_deadband"]
        assert (row["runs"], row["minutes"]) == (1, 20.0)
        assert stats["comfort_kept"]["hits"] == 0
        prune_promise_stats(stats, day(41))
        assert stats == {}

    def test_growth_of_an_open_run_lands_on_the_day_it_was_counted(self):
        stats: dict = {}
        bump_promise_excused(stats, "comfort_kept", "thermostat_deadband", DAY0, 10.0, new_run=True, now=DAY0)
        bump_promise_excused(stats, "comfort_kept", "thermostat_deadband", day(1), 5.0, new_run=False, now=day(1))
        days = stats["comfort_kept"]["excused"]["thermostat_deadband"]["days"]
        assert days == {
            DAY0.date().isoformat(): {"runs": 1, "minutes": 10.0},
            day(1).date().isoformat(): {"runs": 0, "minutes": 5.0},
        }

    def test_the_number_of_excused_reasons_is_bounded(self):
        from custom_components.climate_advisor.promise_journal import MAX_EXCUSED_REASONS  # noqa: PLC0415

        d = day(1).date().isoformat()
        rows = {f"r{i}": {"days": {d: {"runs": 1, "minutes": 1.0}}} for i in range(MAX_EXCUSED_REASONS + 3)}
        out = sanitize_promise_stats({"comfort_kept": {"days": {}, "excused": rows}}, ALLOWED, day(1))
        assert list(out["comfort_kept"]["excused"]) == [f"r{i}" for i in range(MAX_EXCUSED_REASONS)]


# ── the coordinator: summary, save and load ─────────────────────────────────────
def _coordinator_class():
    for mod in ("custom_components.climate_advisor.coordinator",):
        if mod in sys.modules:
            return sys.modules[mod].ClimateAdvisorCoordinator
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: PLC0415

    return ClimateAdvisorCoordinator


def _summary(stats, now):
    c = object.__new__(_coordinator_class())
    c._promise_stats = stats
    with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=now):
        return types.MethodType(_coordinator_class()._promise_summary, c)(), c


class TestSummary:
    def test_summary_is_none_when_the_window_is_empty(self):
        stats: dict = {}
        bump_promise_stats(stats, "settling_flip", DAY0, DAY0)
        summary, c = _summary(stats, day(31))
        assert summary is None
        assert c._promise_stats == {}  # pruned when the summary is built

    def test_summary_counts_only_the_last_thirty_days(self):
        stats: dict = {}
        bump_promise_stats(stats, "settling_flip", DAY0, DAY0)
        bump_promise_stats(stats, "settling_flip", day(20), day(20))
        bump_promise_stats(stats, "observe_only_silence", day(25), day(25))
        summary, _ = _summary(stats, day(31))
        assert summary["total_hits"] == 2
        assert summary["window_days"] == PROMISE_STATS_WINDOW_DAYS
        rows = {r["promise"]: r for r in summary["promises"]}
        assert rows["settling_flip"]["hits"] == 1
        assert rows["settling_flip"]["last_hit"] == day(20).isoformat()
