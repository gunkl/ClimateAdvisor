"""Issue #1527: generated occupant thermostat changes land at a second of the minute, never on it.

Climate Advisor writes on the minute (wake-up, bedtime, the 30-minute cycle) and, by design, reads a mode change within
3 s of its own write as that write reporting back (owner decision D6). A whole-minute occupant change could land in that
same second, where Climate Advisor cannot see it, and the nightly then blamed Climate Advisor for what followed
(run 43037566357, day 17003000036: a false P1 settling_flip). The seconds are drawn last, so nothing else moves.
"""

from __future__ import annotations

from unittest.mock import patch

from tools.sim_harness import generator

SEEDS = [generator.day_seed_of(ms, i) for ms in (101, 102, 17003) for i in range(40)] + [17003000036]


def test_occupant_thermostat_changes_are_off_the_minute_by_whole_seconds() -> None:
    seen = 0
    for ds in SEEDS:
        for minute, kind, _ in generator.build_plan(ds).timed:
            if kind != "thermostat_state_changed":
                continue
            seen += 1
            sec = round(minute * 60) - int(minute) * 60
            assert 5 <= sec <= 55, (ds, minute)
            assert abs(minute * 60 - round(minute * 60)) < 1e-6, (ds, minute)
    assert seen > 20, "the sample must contain occupant thermostat changes"


def test_every_other_draw_of_a_seed_is_unchanged() -> None:
    """Drawn last: the plan built without the seconds draw differs only in those events' fractional minutes."""
    real_randint = generator.random.Random.randint

    for ds in SEEDS[:60]:
        new = generator.build_plan(ds)
        calls = {"n": 0}

        def no_seconds(self, a, b, _calls=calls):  # the #1527 draw is randint(5, 55); every other draw is untouched
            if (a, b) == (5, 55):
                _calls["n"] += 1
                return 0
            return real_randint(self, a, b)

        with patch.object(generator.random.Random, "randint", no_seconds):
            old = generator.build_plan(ds)
        assert [(int(m), k, a) for m, k, a in new.timed] == [(int(m), k, a) for m, k, a in old.timed], ds
        assert (new.config, new.forecast, new.whf) == (old.config, old.forecast, old.whf), ds


def test_the_nightly_day_no_longer_blames_climate_advisor() -> None:
    rec = generator.run_day(17003000036)
    assert not [(h.ts, h.detail) for h in rec.report.hard if h.promise == "settling_flip"]
