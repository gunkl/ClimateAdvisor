"""Generated household days: determinism, replay, physics, and the ability to FIND a broken engine.

Issue #1128 I3.  Occupant first: a generated day is a made-up but plausible household day; if the
generator cannot tell a thermostat that flips heat/cool back and forth from a healthy one, it
gives false comfort.  So besides determinism and replay, this file proves effectiveness: with the
settling guard deliberately removed from the production engine (test-only monkeypatch, the real
code is untouched) the production promise registry must catch a heat/cool flip on a generated day,
and the finding must shrink to a short, replayable story.

Promises are judged only by production code (``invariant_watchdog.run_promise_checks``); nothing
here re-implements a promise.
"""

from __future__ import annotations

import json
import logging
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tools.sim_harness.generator import (
    CI_SMOKE_DAYS,
    CI_SMOKE_SEEDS,
    DAYS_PER_MASTER,
    SIM_HOURS,
    WHF_ORDERS,
    build_plan,
    day_seed_of,
    replay_scenario,
    run_batch,
    run_day,
    run_plan,
    save_finding,
    strip_whf_hazard,
)
from tools.sim_harness.replay import shrink
from tools.sim_harness.world import TICK_MIN, HouseParams, ToyHouse, Weather

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(autouse=True)
def _quiet_logs():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


# ── plans ──────────────────────────────────────────────────────────────────────────────────────
def test_same_seed_same_plan() -> None:
    assert build_plan(1234).to_dict() == build_plan(1234).to_dict()
    assert build_plan(1234).to_dict() != build_plan(1235).to_dict()


def test_day_seed_encodes_master_seed_and_index() -> None:
    assert day_seed_of(3, 7) == 3 * DAYS_PER_MASTER + 7


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_plan(1, "nonsense")


def test_coordinator_plans_always_run_the_30_minute_cycle() -> None:
    """Production always runs the regular coordinator cycle; a day without it gives false findings."""
    for profile in ("coordinator", "mixed"):
        plans = [build_plan(day_seed_of(1, i), profile) for i in range(150)]
        coord = [p for p in plans if p.use_coordinator]
        assert coord
        assert all(p.reclassify_every_min == 30 for p in coord)


def test_plans_span_the_axes() -> None:
    """Generic coverage: the plans vary weather, units, sensors and config without being steered."""
    plans = [build_plan(day_seed_of(1, i)) for i in range(120)]
    highs = [p.forecast["high"] for p in plans]
    assert min(highs) < 50 and max(highs) > 88
    assert {p.unit for p in plans} == {"fahrenheit", "celsius"}
    assert any(p.config["door_window_sensors"] for p in plans)
    assert any("comfort_deadband_warm_f" in p.config for p in plans)
    assert any(p.config.get("aggressive_savings") for p in plans)
    assert {p.config["manual_grace_seconds"] for p in plans} >= {0, 900}


# ── the toy house is physics, not Climate Advisor ──────────────────────────────────────────────
def _house(**kw) -> ToyHouse:
    p = HouseParams(
        k_env_per_hr=kw.get("k", 0.05),
        solar_f_hr=0.0,
        heat_gain_f_hr=4.0,
        cool_gain_f_hr=4.0,
        swing_f=0.5,
        overshoot_f=kw.get("overshoot", 0.0),
        whole_degree=False,
    )
    return ToyHouse(p, kw.get("indoor", 70.0))


def test_house_heats_and_cools_with_hysteresis() -> None:
    h = _house(indoor=66.0)
    assert h.decide_action("heat", 70.0) == "heating"
    for _ in range(40):
        h.step(TICK_MIN, 60.0, 0.0, False)
        h.decide_action("heat", 70.0)
    assert 69.5 <= h.indoor_f <= 71.5  # held around the setpoint, not run away
    c = _house(indoor=80.0)
    assert c.decide_action("cool", 74.0) == "cooling"
    assert c.decide_action("off", 74.0) == "off"
    assert c.running is None


def test_house_drifts_to_outdoor_when_idle_and_faster_with_a_window_open() -> None:
    closed, opened = _house(indoor=72.0), _house(indoor=72.0)
    for _ in range(12):
        closed.step(TICK_MIN, 50.0, 0.0, False)
        opened.step(TICK_MIN, 50.0, 0.0, True)
    assert opened.indoor_f < closed.indoor_f < 72.0


def test_house_coasts_after_the_equipment_cuts_out() -> None:
    h = _house(indoor=70.0, overshoot=0.6)
    h.running = "heating"
    before = h.indoor_f
    h.decide_action("off", None)
    h.coast("heating")
    assert h.indoor_f == pytest.approx(before + 0.6)


def test_weather_is_a_deterministic_diurnal_curve() -> None:
    day0 = datetime(2026, 5, 12, 5, 0)

    def curve() -> list[float]:
        w = Weather(
            random.Random(1), high=90, low=60, tomorrow_high=90, tomorrow_low=60, cloud=0, wobble_f=0.0, day0=day0
        )
        return [w.outdoor_f(datetime(2026, 5, 12, h)) for h in range(5, 24)]

    a = curve()
    assert a == curve()
    assert a.index(max(a)) in range(9, 12)  # peaks mid-afternoon (hours 14-16 from a 05:00 start)
    assert min(a) < 66 and max(a) > 88


# ── running a day ──────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def sample_run():
    return run_day(day_seed_of(1, 3))


def test_a_day_is_closed_loop_and_clean(sample_run) -> None:
    rec = sample_run
    types = [e["type"] for e in rec.scenario["events"]]
    assert types.count("temp_update") >= SIM_HOURS * 60 // TICK_MIN
    assert "classification" in types and "thermostat_state_changed" in types
    assert rec.callback_errors == 0
    assert rec.report.steps > 100


def test_the_classification_event_comes_from_the_real_classifier(sample_run) -> None:
    from custom_components.climate_advisor.classifier import ForecastSnapshot, classify_day

    cls = next(e for e in sample_run.scenario["events"] if e["type"] == "classification")
    f = sample_run.plan.forecast
    real = classify_day(
        ForecastSnapshot(f["high"], f["low"], f["tomorrow_high"], f["tomorrow_low"], f["low"], None, None, None)
    )
    assert (cls["day_type"], cls["hvac_mode"], cls["windows_recommended"]) == (
        real.day_type,
        real.hvac_mode,
        real.windows_recommended,
    )


def test_same_seed_same_scenario_json() -> None:
    a, b = run_day(day_seed_of(2, 5)), run_day(day_seed_of(2, 5))
    assert json.dumps(a.scenario, sort_keys=True) == json.dumps(b.scenario, sort_keys=True)
    assert [h.key for h in a.report.hard] == [h.key for h in b.report.hard]


def test_replay_of_the_saved_scenario_reproduces_the_live_verdict() -> None:
    """The artifact is enough: replaying the recorded events (no generator) gives the same hits."""
    for i in (3, 16, 25):  # includes days that break a promise on HEAD, if any still do
        live = run_day(day_seed_of(1, i))
        replayed = replay_scenario(json.loads(json.dumps(live.scenario)))
        assert sorted(h.key for h in replayed.hard) == sorted(h.key for h in live.report.hard)
        assert sorted(h.key for h in replayed.warnings) == sorted(h.key for h in live.report.warnings)


def test_a_small_batch_has_no_harness_faults_and_varied_days() -> None:
    res = run_batch(4, 12)
    assert res.errors == []
    assert res.callback_error_days == []
    assert res.days == 12
    assert len(res.day_types) >= 3


# ── effectiveness: a deliberately broken engine is FOUND, then shrunk ──────────────────────────
@pytest.fixture()
def settling_guard_removed(monkeypatch):
    """Test-only: make the engine blind to 'the other side just ran' (the shape of #1073).

    The real code is untouched; only these two readers are replaced for the test's duration.
    """
    from custom_components.climate_advisor.automation import AutomationEngine

    monkeypatch.setattr(AutomationEngine, "minutes_since_equipment_ran", lambda self, d, n: None)
    monkeypatch.setattr(AutomationEngine, "minutes_since_mode_set", lambda self, *a, **k: None)


def test_a_broken_engine_is_found_and_the_finding_shrinks(settling_guard_removed, tmp_path) -> None:
    res = run_batch(1, 40, stop_on_promise="settling_flip")
    assert res.hits.get("settling_flip"), "40 generated days found no heat/cool flip with the guard removed"
    day_seed, hit = res.hits["settling_flip"][0]
    rec = run_day(day_seed)
    path = save_finding(rec, tmp_path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["generated"]["found"][0]["promise"] == "settling_flip"
    assert str(path.as_posix()) in saved["generated"]["replay"]

    small = shrink(saved, "settling_flip", max_runs=60)
    assert len(small["events"]) < len(saved["events"]) / 2
    assert any(h.promise == "settling_flip" for h in replay_scenario(small).hard)
    first = datetime.fromisoformat(small["events"][0]["time"]).replace(tzinfo=UTC)
    assert first.date() == hit.ts.date()


def test_the_real_engine_keeps_the_settling_promise_on_the_same_days() -> None:
    res = run_batch(1, 40)
    assert not res.hits.get("settling_flip"), [h.detail for _, h in res.hits.get("settling_flip", [])]


def test_run_plan_is_the_same_as_run_day() -> None:
    plan = build_plan(day_seed_of(5, 1))
    assert run_plan(plan).scenario["events"] == run_day(day_seed_of(5, 1)).scenario["events"]


# Hard promises whose hits on HEAD are NOT yet triaged (reported to the owner with replay seeds).  Everything
# else is a hard failure.  Remove an entry when it is triaged.
# Epic #1125 triage history for ``override_respected`` (10 seeds x 100 days, coordinator profile): 18 days on
# the first measurement, 21 days / 30 hits on the pre-fix tree of Issue #1133.  Every one was a real Climate
# Advisor defect or an owner decision, all fixed together: #1131 (setpoint change during a door pause),
# #1132 (door pause not recorded), #1133 (grace-0 override paused by a later window), #1146 (setpoint-only
# override lost its grace flags), #1147 (nat-vent exit rewrote the occupant's Off) and #1148 (the #655 bypass
# paused a protected manual grace).  The same 10 x 100 run is now 0 hits, so the tolerance is gone: any new
# ``override_respected`` hit is a failure.
_UNTRIAGED_ON_HEAD: set[str] = set()

# Nightly / epic-#1125 seeds that each broke ``override_respected`` before the "protect the occupant's manual
# change" fixes (#1131 #1133 #1146 #1147 #1148); the issue named in each entry is the one the issue text
# attributes it to, the rest are nightly seeds the family clears together.
_PROTECT_MANUAL_CHANGE_SEEDS = {
    1000016: "#1131 setpoint change during a door pause overwritten at the door close",
    1000025: "#1133 grace 0, window opened after a confirmed override (the #1153 mechanism example)",
    3000032: "#1131 (3000032 is the second #1131 example)",
    1003000002: "#1146 setpoint-only override lost its grace flags",
    1008000153: "#1146",
    1012000126: "#1146",
    1012000229: "#1146",
    1007000194: "#1148 #655 bypass fell through to a pause over a manual grace",
    1005000246: "#1147 nat-vent exit rewrote the occupant's Off",
    1002000218: "nightly seed; cleared by the family, not bisected to one issue",
    1001000043: "nightly seed; cleared by the family, not bisected to one issue",
    1002000019: "nightly seed; cleared by the family, not bisected to one issue",
    1004000122: "nightly seed; cleared by the family, not bisected to one issue",
    1006000028: "nightly seed; cleared by the family, not bisected to one issue",
    1012000033: "nightly seed; cleared by the family, not bisected to one issue",
}


@pytest.mark.parametrize("day_seed", sorted(_PROTECT_MANUAL_CHANGE_SEEDS))
def test_the_occupants_manual_change_is_respected_on_the_days_that_used_to_break_it(day_seed: int) -> None:
    rec = run_day(day_seed, "coordinator")
    broken = [(h.promise, h.detail) for h in rec.report.hard]
    assert not broken, f"seed {day_seed} ({_PROTECT_MANUAL_CHANGE_SEEDS[day_seed]}): {broken}"


@pytest.mark.parametrize("master_seed", CI_SMOKE_SEEDS)
def test_smoke_generated_days_keep_the_household_promises(master_seed: int) -> None:
    """The CI-sized random-day run: fixed seeds, no harness faults, no un-triaged hard promise broken.

    The same seeds on the pre-fix #1073 tree break ``settling_flip`` (trial gate); on HEAD they must not.
    """
    res = run_batch(master_seed, CI_SMOKE_DAYS)
    assert res.errors == [] and res.callback_error_days == []
    broken = {k: [(d, h.detail) for d, h in v[:2]] for k, v in res.hits.items() if k not in _UNTRIAGED_ON_HEAD}
    assert not broken, f"a hard household promise broke on generated days (replay: --generate-day <seed>): {broken}"


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# Hazards: the occupant switches automation off; the indoor temperature source drops out
# (Issue #1128 follow-up: without these, no generated day could ever break promise 3 or promise 5)
# ═══════════════════════════════════════════════════════════════════════════════════════════════════
_HAZARDS = ("automation_toggle", "indoor_unreadable")
# sha256 of the plans of seeds 0..59 x 3 profiles with the hazard events removed, taken BEFORE the
# hazards existed.  New draws are appended at the end of the plan's random stream, so every earlier
# draw (and every seed's earlier events) must stay identical; reordering a draw breaks this.
_PRE_HAZARD_PLAN_DIGEST = "1837d3244ca6cd245530d8d03dee7d20c991360f50f6d0e5605b2294e77e3e63"


def test_adding_hazards_left_every_earlier_draw_unchanged() -> None:
    import hashlib

    h = hashlib.sha256()
    for i in range(60):
        for profile in ("coordinator", "engine", "mixed"):
            d = build_plan(i, profile).to_dict()
            d = strip_whf_hazard(d)  # Issue #1144: the whole-house-fan day is a later, appended draw
            d["timed"] = [t for t in d["timed"] if t[1] not in _HAZARDS]
            h.update(json.dumps(d, sort_keys=True).encode())
    assert h.hexdigest() == _PRE_HAZARD_PLAN_DIGEST


def test_hazards_are_drawn_at_low_rates_and_come_in_pairs() -> None:
    plans = [build_plan(day_seed_of(11, i), "mixed") for i in range(400)]

    def kinds(p):
        return [(k, a) for _, k, a in p.timed if k in _HAZARDS]

    off = [p for p in plans if any(k == "automation_toggle" and not a["enabled"] for k, a in kinds(p))]
    out = [p for p in plans if any(k == "indoor_unreadable" and a["unreadable"] for k, a in kinds(p))]
    assert 0.02 * len(plans) <= len(off) <= 0.15 * len(plans)
    assert 0.02 * len(plans) <= len(out) <= 0.15 * len(plans)
    for p in out:  # every outage has its recovery, after it starts
        starts = [m for m, k, a in p.timed if k == "indoor_unreadable" and a["unreadable"]]
        ends = [m for m, k, a in p.timed if k == "indoor_unreadable" and not a["unreadable"]]
        assert len(starts) == len(ends) == 1 and ends[0] > starts[0]


def _hazard_scenario(events: list[dict], **config) -> dict:
    return {
        "name": "hazard_probe",
        "config": {
            "comfort_heat": 68,
            "comfort_cool": 76,
            "sleep_heat": 64,
            "sleep_cool": 78,
            "sleep_time": "22:30",
            "wake_time": "06:30",
            **config,
        },
        "skip_startup_coalesce": True,
        "events": events,
    }


_OFF_THEN_BEDTIME = [
    {"time": "2026-05-12T20:00:00", "type": "temp_update", "indoor_f": 70.0, "outdoor_f": 50.0},
    {
        "time": "2026-05-12T20:01:00",
        "type": "classification",
        "day_type": "cool",
        "hvac_mode": "heat",
        "windows_recommended": False,
        "today_high": 55,
        "today_low": 40,
        "tomorrow_high": 55,
        "tomorrow_low": 40,
    },
    {"time": "2026-05-12T21:00:00", "type": "automation_toggle", "enabled": False},
    {"time": "2026-05-12T22:30:00", "type": "bedtime"},
]


def _run_scenario(scenario: dict, *, coordinator: bool):
    from tools.sim_harness.promise_probe import PromiseProbe
    from tools.sim_harness.run_production import run_production_scenario

    return run_production_scenario(
        scenario, use_coordinator=coordinator, probe=PromiseProbe(scenario_name=scenario["name"])
    )


def test_automation_toggle_reaches_the_real_switch_state_the_way_production_does() -> None:
    from tools.sim_harness.session import make_session

    off = {"time": "2026-05-12T21:00:00", "type": "automation_toggle", "enabled": False}
    on = {"time": "2026-05-12T21:30:00", "type": "automation_toggle", "enabled": True}
    for use_coordinator in (True, False):
        sc = _hazard_scenario(_OFF_THEN_BEDTIME[:2])
        session = make_session(sc, engine="fakehass", use_coordinator=use_coordinator)
        try:
            session.advance_to(datetime(2026, 5, 12, 21, 0, tzinfo=UTC))
            session.feed(off)
            assert session.engine.dry_run is True
            if use_coordinator:
                assert session.coordinator.automation_enabled is False
            session.feed(on)
            assert session.engine.dry_run is False
            if use_coordinator:
                assert session.coordinator.automation_enabled is True
        finally:
            session.close()


def test_indoor_unreadable_blinds_the_engine_and_recovery_restores_it() -> None:
    from tools.sim_harness.session import make_session

    base = {"time": "2026-05-12T20:00:00", "type": "temp_update", "indoor_f": 71.0, "outdoor_f": 50.0}
    back = {"time": "2026-05-12T20:30:00", "type": "indoor_unreadable", "unreadable": False, "indoor_f": 72.5}
    for use_coordinator in (True, False):
        session = make_session(_hazard_scenario([base]), engine="fakehass", use_coordinator=use_coordinator)
        try:
            session.feed(base)
            assert session.engine._get_indoor_temp_f() == pytest.approx(71.0)
            session.feed({"time": "2026-05-12T20:05:00", "type": "indoor_unreadable"})
            assert session.engine._get_indoor_temp_f() is None
            assert session.thermostat().indoor_f is None
            session.feed(back)
            assert session.engine._get_indoor_temp_f() == pytest.approx(72.5)
        finally:
            session.close()


# ── effectiveness: P3 (observe-only silence) ──────────────────────────────────────────────────────
@pytest.fixture()
def observe_only_gate_removed(monkeypatch):
    """Test-only: the engine no longer knows it is observe-only (every ``dry_run`` check passes).

    The real code is untouched.  The probe reads the switch from the coordinator
    (``coordinator.automation_enabled``), so it still sees the occupant's real choice.
    """
    from custom_components.climate_advisor.automation import AutomationEngine

    monkeypatch.setattr(AutomationEngine, "dry_run", property(lambda self: False, lambda self, v: None), raising=False)


def test_p3_real_engine_stays_silent_after_the_switch_is_turned_off() -> None:
    res = _run_scenario(_hazard_scenario(_OFF_THEN_BEDTIME), coordinator=True)
    assert res.promise_report.hard == []
    set_temps = [a["data"].get("temperature") for a in res.action_log if a["service"] == "set_temperature"]
    assert 64.0 not in set_temps  # the bedtime setback was NOT sent


def test_p3_fires_when_the_observe_only_gate_is_removed(observe_only_gate_removed) -> None:
    res = _run_scenario(_hazard_scenario(_OFF_THEN_BEDTIME), coordinator=True)
    assert [h.promise for h in res.promise_report.hard] == ["observe_only_silence"]
    set_temps = [a["data"].get("temperature") for a in res.action_log if a["service"] == "set_temperature"]
    assert 64.0 in set_temps  # the setback went out while the occupant had automation off


def test_p3_is_reachable_by_generated_days(observe_only_gate_removed) -> None:
    """The generator itself (no steering) draws a day on which a gate-less engine breaks promise 3."""
    found = None
    tried = 0
    for i in range(400):
        plan = build_plan(day_seed_of(9, i), "coordinator")
        if not any(k == "automation_toggle" and not a["enabled"] for _, k, a in plan.timed):
            continue
        tried += 1
        if "observe_only_silence" in run_plan(plan).found:
            found = i
            break
        if tried >= 40:
            break
    assert found is not None, f"{tried} generated days with the switch off never exposed a gate-less engine"


def test_p3_never_fires_on_generated_days_for_the_real_engine() -> None:
    n = 0
    for i in range(400):
        plan = build_plan(day_seed_of(9, i), "coordinator")
        if not any(k == "automation_toggle" and not a["enabled"] for _, k, a in plan.timed):
            continue
        rec = run_plan(plan)
        assert "observe_only_silence" not in rec.found, f"day {i}"
        assert rec.callback_errors == 0
        n += 1
        if n >= 20:
            break


# ── effectiveness: P5 (no fan start while the indoor temperature is unreadable) ───────────────────
@pytest.fixture()
def blind_fan_guard_removed(monkeypatch):
    """Test-only: the engine acts on a stale reading instead of refusing when indoor is unreadable.

    The shape of "fail open" (#1042): the real code is untouched.  Only the engine's own callers get
    the stale value; the probe (which asks what the engine could actually see) still sees "unreadable".
    """
    import sys

    from custom_components.climate_advisor.automation import AutomationEngine

    real = AutomationEngine._get_indoor_temp_f

    def stale_on_blind(self, *a, **k):
        value = real(self, *a, **k)
        if value is not None:
            self.__dict__["_test_last_good_indoor"] = value
            return value
        if sys._getframe(1).f_globals.get("__name__", "").endswith("promise_probe"):
            return None
        return self.__dict__.get("_test_last_good_indoor")

    monkeypatch.setattr(AutomationEngine, "_get_indoor_temp_f", stale_on_blind)


def _wh_fan_golden_with_blind_window() -> dict:
    """The whole-house-fan golden (read only), with the indoor sensor dropping out before the window opens."""
    path = Path(__file__).resolve().parent.parent / "tools" / "simulations" / "golden"
    sc = json.loads((path / "whole_house_fan_hvac_suppression.json").read_text(encoding="utf-8"))
    sc["events"].insert(2, {"time": "2026-06-12T07:30:00", "type": "indoor_unreadable"})
    return sc


def test_p5_real_engine_does_not_start_the_fan_blind() -> None:
    res = _run_scenario(_wh_fan_golden_with_blind_window(), coordinator=False)
    assert res.promise_report.hard == []
    assert not [a for a in res.action_log if a["domain"] == "fan" and a["service"] == "turn_on"]


def test_p5_fires_when_the_blind_fan_guard_is_removed(blind_fan_guard_removed) -> None:
    res = _run_scenario(_wh_fan_golden_with_blind_window(), coordinator=False)
    assert [h.promise for h in res.promise_report.hard] == ["no_blind_fan_start"]
    assert [a for a in res.action_log if a["domain"] == "fan" and a["service"] == "turn_on"]


def test_p5_is_reachable_by_generated_days(blind_fan_guard_removed) -> None:
    found = None
    tried = 0
    for i in range(600):
        plan = build_plan(day_seed_of(9, i), "engine")
        if not any(k == "indoor_unreadable" for _, k, _a in plan.timed):
            continue
        tried += 1
        if "no_blind_fan_start" in run_plan(plan).found:
            found = i
            break
        if tried >= 90:
            break
    assert found is not None, f"{tried} generated days with an outage never exposed a fail-open engine"


def test_p5_never_fires_on_generated_days_for_the_real_engine() -> None:
    n = 0
    for i in range(400):
        plan = build_plan(day_seed_of(9, i), "engine")
        if not any(k == "indoor_unreadable" for _, k, _a in plan.timed):
            continue
        rec = run_plan(plan)
        assert "no_blind_fan_start" not in rec.found, f"day {i}"
        assert rec.callback_errors == 0
        n += 1
        if n >= 25:
            break


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# Whole-house-fan days (Issue #1144): the occupant switches the fan on around the doors and windows
# ═══════════════════════════════════════════════════════════════════════════════════════════════════
def _whf_plans(n: int = 300):
    return [p for p in (build_plan(day_seed_of(21, i), "coordinator") for i in range(n)) if p.whf]


def test_whf_days_are_drawn_at_the_set_rate_and_cover_every_order() -> None:
    plans = [build_plan(day_seed_of(21, i), "coordinator") for i in range(300)]
    whf = [p for p in plans if p.whf]
    assert 0.2 * len(plans) <= len(whf) <= 0.5 * len(plans)
    assert {p.whf["order"] for p in whf} == set(WHF_ORDERS)
    assert {p.config["fan_mode"] for p in whf} == {"whole_house_fan", "both"}
    assert all(p.config["door_window_sensors"] and p.config["fan_state_feedback"] for p in whf)
    # the engine-only profile has no fan-entity listener: never drawn there
    assert not [p for p in (build_plan(day_seed_of(21, i), "engine") for i in range(100)) if p.whf]


def test_whf_order_timing_matches_the_order_name() -> None:
    for p in _whf_plans():
        on = next(m for m, k, a in p.timed if k == "external_fan_state_change" and a["state"] == "on")
        closes = [m for m, k, _ in p.timed if k == "sensor_close" and abs(m - (on - 1.0)) < 1e-9]
        opens_after = [m for m, k, _ in p.timed if k == "sensor_open" and 0.0 < m - on <= 0.5 + 1e-9]
        order = p.whf["order"]
        if order == "fan_on_then_open_fast":
            assert (
                closes
                and [round((m - on) * 60) for m in opens_after]
                and all(0 < (m - on) * 60 < 10 for m in opens_after)
            )
        elif order == "fan_on_then_open_late":
            assert closes and opens_after and all(10 <= round((m - on) * 60) < 30 for m in opens_after)
        elif order == "sealed_then_fan_on":
            assert closes
        else:  # fan_on_then_sealed: a window is open when the fan starts, the house seals after
            assert any(m == on - 1.0 for m, k, _ in p.timed if k == "sensor_open")


def test_a_sealed_house_fan_day_never_breaks_the_whf_promises_on_head() -> None:
    sealed = [p for p in _whf_plans(120) if p.whf["order"] in ("sealed_then_fan_on", "fan_on_then_sealed")][:3]
    assert sealed
    for p in sealed:
        rec = run_plan(p)
        bad = [h for h in rec.report.hard if h.promise in ("whf_sealed", "ac_whf_mutex")]
        assert not bad, f"seed {p.day_seed} ({p.whf['order']}): {[h.detail for h in bad]}"


def test_seed_7000031_fan_pressed_inside_the_restart_window_no_longer_overlaps_the_ac() -> None:
    """Calibration finding (Issue #1144): fan switched on within startup coalescing while the AC cools.

    Before the fix the enforcer could not see the fan-on as newer than the AC (override detection is suppressed
    while coalescing), so the AC ran beside the fan for the full five-minute window.
    """
    rec = run_day(7000031)
    assert rec.plan.whf is not None
    assert [h for h in rec.report.hard if h.promise == "ac_whf_mutex"] == []
