"""Issues #1411 / #1412: the comfort-kept promise (P7) must know the house is Away.

Occupant view: on a normal Away day the house sits at 64 F, inside its Away band (60-80 F), and nobody is home. The
comfort-kept check (which also runs log-only at home) reported "The house stayed 3.0°F outside your comfort range for
50 minutes while Climate Advisor was in control", judging those Away hours against the home floor (68 - 1 F slack).

Two causes, both fixed:

* #1411: P7 knew Away only from an ``occupancy_setback`` journal mark. None is journaled when Away is set before the
  first classification (the handler has no band to write yet). Now every ``Reading`` carries the occupancy mode
  Climate Advisor acts on (``Reading.occupancy``) and P7 decides Away / Vacation from it.
* #1412: even with a setback mark, the mark ages out of the 3 h window while the Away readings it governed are still
  in it, so the away timeline started the window as "home". The journal now hands back the newest state mark that
  left the window (``PromiseInput.carried_state_marks``), for the away and the nat-vent timelines.

Each fix is proved on its own (a source without occupancy still needs the carried mark), and the controls prove a
real miss after arrival home is still caught.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from custom_components.climate_advisor.invariant_watchdog import (
    SCOPE_HOME,
    SCOPE_TEST,
    PromiseConfig,
    check_p7_comfort_kept,
    run_promise_checks,
)
from custom_components.climate_advisor.promise_journal import (
    JOURNAL_MAX_AGE,
    MAX_MARKS,
    PromiseJournal,
    Reading,
)

T0 = datetime(2026, 1, 13, 13, 0, tzinfo=UTC)
CFG = PromiseConfig.from_config({"comfort_heat": 68, "comfort_cool": 76})
GOLDEN = Path(__file__).resolve().parent.parent / "tools" / "simulations" / "golden"


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def rd(minutes: float, *, occupancy: str | None, indoor: float = 64.0, mode: str = "heat") -> Reading:
    """Heat set to the Away pre-heat target 63 F, idle, room 64 F: inside the Away band, 3 F below the home floor."""
    return Reading(
        ts=at(minutes),
        hvac_mode=mode,
        hvac_action="idle",
        indoor_f=indoor,
        outdoor_f=30.0,
        forecast_high_f=40.0,
        setpoint_f=63.0,
        fan_on=None,
        contacts_open=None,
        occupancy=occupancy,
    )


def _journal(readings, events=()) -> PromiseJournal:
    j = PromiseJournal()
    for m, etype, payload in events:
        j.on_event(at(m), etype, payload)
    for r in readings:
        j.record_reading(r)
    return j


def _hits(j: PromiseJournal, now_min: float, cfg: PromiseConfig = CFG):
    return check_p7_comfort_kept(j.window(at(now_min)), cfg)


class TestAwayFromTheOccupancyMode:
    """#1411: Away set before the first classification journals no setback; the reading says Away."""

    def test_away_hours_without_a_setback_mark_are_not_a_comfort_miss(self) -> None:
        j = _journal([rd(m, occupancy="away") for m in range(5, 161, 5)])
        assert _hits(j, 160) == []

    def test_negative_control_the_same_hours_at_home_are_a_miss(self) -> None:
        """Effectiveness: the readings ARE outside the home range, so only the occupancy mode excuses them."""
        j = _journal([rd(m, occupancy="home") for m in range(5, 161, 5)])
        (h,) = _hits(j, 160)
        assert dict(h.evidence)["side"] == "below" and dict(h.evidence)["worst_f"] == pytest.approx(3.0)

    def test_vacation_is_unoccupied_too(self) -> None:
        assert _hits(_journal([rd(m, occupancy="vacation") for m in range(5, 161, 5)]), 160) == []

    def test_guest_is_occupied(self) -> None:
        assert len(_hits(_journal([rd(m, occupancy="guest") for m in range(5, 161, 5)]), 160)) == 1

    def test_a_real_miss_after_coming_home_is_still_caught(self) -> None:
        """Silent-failure guard: the excuse ends the minute Climate Advisor is Home again (no heat running here)."""
        rs = [rd(m, occupancy="away") for m in range(5, 61, 5)] + [rd(m, occupancy="home") for m in range(65, 161, 5)]
        (h,) = _hits(_journal(rs), 160)
        assert h.key.start == at(65)

    def test_the_reading_wins_over_a_stale_mark(self) -> None:
        """A setback mark with no restore mark does not excuse readings that say Climate Advisor is Home."""
        rs = [rd(m, occupancy="home") for m in range(5, 161, 5)]
        j = _journal(rs, [(1, "occupancy_setback", {"mode": "away"})])
        assert len(_hits(j, 160)) == 1

    def test_celsius_install(self) -> None:
        cfg = PromiseConfig.from_config({"comfort_heat": 68, "comfort_cool": 76, "temp_unit": "celsius"})
        assert _hits(_journal([rd(m, occupancy="away") for m in range(5, 161, 5)]), 160, cfg) == []
        (h,) = _hits(_journal([rd(m, occupancy="home") for m in range(5, 161, 5)]), 160, cfg)
        assert "1.7°C" in h.detail  # 3 F below, said in the occupant's unit

    def test_a_change_of_occupancy_is_stored_at_once(self) -> None:
        j = PromiseJournal()
        assert j.record_reading(rd(0, occupancy="away"))
        assert not j.record_reading(rd(1, occupancy="away"))  # nothing changed, inside 5 min
        assert j.record_reading(rd(2, occupancy="home"))


class TestStateCarriedPastTheWindow:
    """#1412: a source with no occupancy on its readings still keeps the Away state past the 3 h window."""

    def _away_day(self):
        rs = [rd(m, occupancy=None) for m in range(5, 161, 5)]
        return rs, [(0.02, "occupancy_setback", {"mode": "away"}), (160, "occupancy_comfort_restored", {})]

    def test_a_setback_that_aged_out_still_excuses_the_away_readings(self) -> None:
        rs, ev = self._away_day()
        j = _journal(rs, ev)
        now = 5 + JOURNAL_MAX_AGE.total_seconds() / 60  # 13:00:01 has left the window; 13:05.. has not
        inp = j.window(at(now))
        assert [m.kind for m in inp.marks] == ["occupancy_comfort_restored"]
        assert [m.kind for m in inp.carried_state_marks] == ["occupancy_setback"]
        assert check_p7_comfort_kept(inp, CFG) == []

    def test_negative_control_without_the_setback_the_same_window_is_a_miss(self) -> None:
        rs, _ = self._away_day()
        j = _journal(rs, [(160, "occupancy_comfort_restored", {})])
        assert len(_hits(j, 5 + JOURNAL_MAX_AGE.total_seconds() / 60)) == 1

    def test_a_restore_that_aged_out_carries_home(self) -> None:
        rs = [rd(m, occupancy=None) for m in range(60, 221, 5)]
        j = _journal(rs, [(0.02, "occupancy_setback", {}), (30, "occupancy_comfort_restored", {})])
        inp = j.window(at(220))
        assert [m.kind for m in inp.carried_state_marks] == ["occupancy_comfort_restored"]
        assert len(check_p7_comfort_kept(inp, CFG)) == 1

    def test_the_mark_cap_does_not_drop_the_state(self) -> None:
        j = PromiseJournal()
        j.on_event(at(0), "occupancy_setback", {"mode": "away"})
        for i in range(MAX_MARKS):
            j.on_event(at(1 + i * 0.01), "grace_expired", {"source": "manual"})
        for r in [rd(m, occupancy=None) for m in range(5, 61, 5)]:
            j.record_reading(r)
        inp = j.window(at(60))
        assert all(m.kind != "occupancy_setback" for m in inp.marks)
        assert [m.kind for m in inp.carried_state_marks] == ["occupancy_setback"]
        assert check_p7_comfort_kept(inp, CFG) == []

    def test_a_restart_forgets_the_carried_state(self) -> None:
        j = _journal([], [(0, "occupancy_setback", {})])
        j.window(at(400))
        assert j.window(at(400)).carried_state_marks
        j.reset()
        assert j.window(at(400)).carried_state_marks == ()

    def test_mirror_a_long_nat_vent_session_stays_excused(self) -> None:
        """The same aging hit the nat-vent timeline: a session that opened before the window still excuses P7."""
        rs = [rd(m, occupancy="home", indoor=79.0, mode="cool") for m in range(5, 161, 5)]
        start = [(0.02, "nat_vent_activated", {})]
        now = 5 + JOURNAL_MAX_AGE.total_seconds() / 60
        assert _hits(_journal(rs, start), now) == []
        assert len(_hits(_journal(rs), now)) == 1  # control: no session, a real miss

    def test_the_carried_marks_never_reach_the_window_marks(self) -> None:
        """Other promises read ``marks``; only P7's state timelines read the carried ones."""
        j = _journal([], [(0, "nat_vent_activated", {})])
        inp = j.window(at(0) + JOURNAL_MAX_AGE + timedelta(minutes=1))
        assert inp.marks == () and [m.kind for m in inp.carried_state_marks] == ["nat_vent_activated"]


# ── end to end, through the real engine and coordinator ────────────────────────────────────────────────────
def _run_golden(*, classify_first: bool, occupancy_on_readings: bool, monkeypatch):
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: PLC0415
    from tools.sim_harness import fake_hass as _fh  # noqa: PLC0415
    from tools.sim_harness import promise_probe as pp  # noqa: PLC0415
    from tools.sim_harness.run_production import run_production_scenario  # noqa: PLC0415

    # Issue #1537: a 0.5 F thermostat swing, so the Away setback below the room stops the furnace (see the negative
    # control's docstring); with the 1.5 F harness default P7 would excuse every reading as a running furnace.
    monkeypatch.setattr(_fh, "THERMOSTAT_ACTION_DEADBAND_F", 0.5)
    if not occupancy_on_readings:
        monkeypatch.setattr(pp.FakeHassPromiseSource, "_occupancy", lambda self: None)
        monkeypatch.setattr(ClimateAdvisorCoordinator, "_promise_occupancy", lambda self: None)
    scenario = json.loads((GOLDEN / "tou_away_heat_bank_home_return_1404.json").read_text(encoding="utf-8"))
    if classify_first:
        # The #1412 scratch variant: a classification before the Away event, so the setback IS journaled.
        scenario["events"].insert(
            1,
            {
                "time": "2026-01-13T12:59:00",
                "type": "classification",
                "day_type": "cold",
                "hvac_mode": "heat",
                "windows_recommended": False,
            },
        )
    probe = pp.PromiseProbe(scenario_name="p7_away_1411")
    seen: list[str] = []
    real_on_event = probe.journal.on_event

    def spy(ts, etype, payload=None):
        seen.append(etype)
        return real_on_event(ts, etype, payload)

    probe.journal.on_event = spy
    result = run_production_scenario(scenario, use_coordinator=True, probe=probe)
    rep = result.promise_report
    return [h for h in rep.warnings + rep.hard + rep.waived if h.promise == "comfort_kept"], seen


@pytest.mark.parametrize("occupancy_on_readings", [True, False], ids=["with_occupancy", "marks_only"])
def test_away_with_a_journaled_setback_is_not_a_miss_end_to_end(occupancy_on_readings, monkeypatch) -> None:
    """#1412: setback journaled at 13:00:01; either fix alone keeps the Away hours out of P7."""
    hits, seen = _run_golden(classify_first=True, occupancy_on_readings=occupancy_on_readings, monkeypatch=monkeypatch)
    assert "occupancy_setback" in seen  # precondition: this is the journaled-setback case
    assert hits == []


def test_away_before_the_first_classification_is_not_a_miss_end_to_end(monkeypatch) -> None:
    """#1411: the golden as written (no setback journaled); the occupancy on each reading carries it."""
    hits, seen = _run_golden(classify_first=False, occupancy_on_readings=True, monkeypatch=monkeypatch)
    assert "occupancy_setback" not in seen
    assert hits == []


def test_negative_control_end_to_end_without_either_fix_the_false_miss_returns(monkeypatch) -> None:
    """Effectiveness: no occupancy on readings and no carried mark reproduces the reported 3.0 F / 50 min line.

    Issue #1537: the harness thermostat now reports hvac_action, and P7 never judges a reading whose furnace is
    reported running. In the reported incident the furnace was idle at 64 F under a 63 F Away setpoint. With the
    harness's 1.5 F swing the scripted room (which never warms) leaves the furnace that started at 12:59 "heating"
    until 64.5 F, so ``_run_golden`` runs the thermostat with a 0.5 F swing: the setback below the room stops the
    furnace at once, as on the incident's thermostat (and the positive tests are judged on the same idle
    furnace). P7 equipment-unknown cannot reproduce it either: such a reading is not judged.
    """
    from custom_components.climate_advisor.promise_journal import PromiseJournal as PJ  # noqa: PLC0415

    monkeypatch.setattr(PJ, "_leaves_window", lambda self, mark: None)
    hits, _ = _run_golden(classify_first=True, occupancy_on_readings=False, monkeypatch=monkeypatch)
    assert len(hits) == 1 and "3.0°F" in hits[0].detail


def test_home_scope_runs_the_same_rule() -> None:
    """The production watchdog (scope home) and the tests share the rule: no false Away miss at home either."""
    j = _journal([rd(m, occupancy="away") for m in range(5, 161, 5)])
    inp = j.window(at(160))
    assert [h for h in run_promise_checks(inp, CFG, scope=SCOPE_HOME) if h.promise == "comfort_kept"] == []
    assert [h for h in run_promise_checks(inp, CFG, scope=SCOPE_TEST) if h.promise == "comfort_kept"] == []
