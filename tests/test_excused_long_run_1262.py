"""Issue #1262: a long continuous thermostat-deadband stretch is counted in full, not capped at the 3 h window.

The occupant keeps seeing the P7 exception for as long as it lasts (the whole point of #1260's visible excused
time); a 12 h stretch used to read 180 min and ``last_ts`` stopped moving. These tests drive the real watchdog
check and the real journal exactly as the coordinator does (reading -> window -> excused records -> progress ->
``bump_promise_excused``), with no logic re-implemented in the test body.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import pytest

if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

from custom_components.climate_advisor import invariant_watchdog as iw  # noqa: E402
from custom_components.climate_advisor.promise_journal import (  # noqa: E402
    PromiseJournal,
    Reading,
    bump_promise_excused,
)

T0 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
CFG = iw.PromiseConfig()
FLOOR = CFG.floor_lo(T0)
STEP = 5


def _reading(minute: int, *, in_range: bool = False) -> Reading:
    # 1.4 F below the heat floor, heat idle, thermostat set at the floor: the thermostat's own deadband.
    indoor = FLOOR + 1.0 if in_range else FLOOR - 1.4
    return Reading(
        ts=T0 + timedelta(minutes=minute),
        hvac_mode="heat",
        hvac_action="idle",
        indoor_f=indoor,
        setpoint_f=FLOOR,
        fan_on=False,
        contacts_open=False,
    )


class _Home:
    """The coordinator's excused bookkeeping, fed one reading at a time."""

    def __init__(self, journal: PromiseJournal | None = None, stats: dict | None = None) -> None:
        self.journal = journal or PromiseJournal()
        self.stats: dict = stats if stats is not None else {}

    def tick(self, minute: int, **kw) -> None:
        r = _reading(minute, **kw)
        self.journal.record_reading(r)
        for ex in iw.run_promise_excused(self.journal.window(r.ts), CFG, scope=iw.SCOPE_HOME):
            new_run, added = self.journal.excused_progress(ex.key, ex.intervals)
            if new_run or added > 0:
                bump_promise_excused(self.stats, ex.promise, ex.excused_by, ex.ts, added, new_run=new_run, now=ex.ts)

    def run_for(self, minutes: int, start: int = 0) -> None:
        for m in range(start, minutes + 1, STEP):
            self.tick(m)

    @property
    def row(self) -> dict:
        return self.stats["comfort_kept"]["excused"]["thermostat_deadband"]


@pytest.mark.parametrize("hours", [4, 12, 24])
def test_a_long_continuous_stretch_is_counted_in_full(hours):
    home = _Home()
    home.run_for(hours * 60)
    assert home.row["minutes"] == pytest.approx(hours * 60.0)
    assert home.row["runs"] == 1


@pytest.mark.parametrize("hours", [4, 12])
def test_last_ts_keeps_advancing_through_a_long_stretch(hours):
    home = _Home()
    home.run_for(hours * 60)
    assert home.row["last_ts"] == (T0 + timedelta(minutes=hours * 60)).isoformat()


@pytest.mark.parametrize("hours", [1, 2, 3])
def test_short_stretches_are_unchanged(hours):
    home = _Home()
    home.run_for(hours * 60)
    assert home.row["minutes"] == pytest.approx(hours * 60.0)
    assert home.row["runs"] == 1


def test_re_evaluating_the_same_window_adds_nothing():
    home = _Home()
    home.run_for(300)
    before = dict(home.row)
    home.tick(300)  # same instant again
    assert home.row["minutes"] == pytest.approx(before["minutes"])
    assert home.row["runs"] == before["runs"]


def test_restart_mid_stretch_does_not_double_count():
    """Restart drops the journal's evidence (and so its counted_until); the stats persist."""
    home = _Home()
    home.run_for(300)
    persisted = {"comfort_kept": {**home.stats["comfort_kept"], "excused": {"thermostat_deadband": dict(home.row)}}}
    before = home.row["minutes"]
    restarted = _Home(stats=persisted)  # fresh journal, restored stats
    restarted.run_for(720, start=305)
    # Never more than the true stretch: the pre-restart 300 min are not counted again.
    assert restarted.row["minutes"] <= 720.0 + 1e-6
    assert restarted.row["minutes"] >= before
    assert restarted.row["minutes"] == pytest.approx(before + (720 - 305))
    assert restarted.row["runs"] == 2  # the post-restart stretch opens its own run


def test_journal_reset_mid_stretch_does_not_double_count():
    home = _Home()
    home.run_for(300)
    home.journal.reset()
    home.run_for(720, start=305)
    assert home.row["minutes"] <= 720.0 + 1e-6


def test_a_broken_stretch_starts_a_new_run():
    home = _Home()
    home.run_for(240)
    home.tick(245, in_range=True)  # the room is back in range: the stretch ends
    home.run_for(600, start=250)
    assert home.row["runs"] == 2
    # 245 min up to the in-range reading (the last excused state holds until it arrives) + 350 min after it.
    assert home.row["minutes"] == pytest.approx(245.0 + 350.0)
