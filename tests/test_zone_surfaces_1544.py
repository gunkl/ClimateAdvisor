"""Issue #1544 phase 2 (#1488, #1542): no promise, plan step or judgement about a mode the zone cannot use now.

Occupant view: a home with electric baseboards (``[off, heat]``, #1348) on a 92 °F day is told to close up and draw the
blinds -- never "I'll handle the AC transition" or "Tonight I'll pre-cool"; its day plan shows the heat held at the
floor and no AC run; a hot afternoon is "outside what the equipment can do", never a Climate Advisor failure. A
cooling-only home gets the mirror ("close up, the sun is your heat"), and a seasonal home whose cooling is locked out
(outdoor lockout or the Heat-only season, #1699) is treated as heat-only for everything it is told.

Every surface reads ``AutomationEngine.zone_service()`` (served = available and not policy-locked).
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.climate_advisor import operation_timeline as ot
from custom_components.climate_advisor import zone_capability as zc
from custom_components.climate_advisor.briefing import generate_briefing
from custom_components.climate_advisor.classifier import DayClassification
from tests.timeline_fixtures import make_snapshot
from tools.sim_harness.build_engine import build_headless_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

HEAT_ONLY = zc.ZoneService(
    heat=True, cool=False, cool_reason=zc.REASON_EQUIPMENT, defended_heat=True, defended_cool=False
)
COOL_ONLY = zc.ZoneService(
    heat=False, cool=True, heat_reason=zc.REASON_EQUIPMENT, defended_heat=False, defended_cool=True
)
COOL_LOCKED = zc.ZoneService(
    heat=True, cool=False, cool_reason=zc.REASON_OUTDOOR_LOCKOUT, defended_heat=True, defended_cool=False
)
# The cool lockout stepping aside for the safety release: not served (never planned or promised), defended (judged).
COOL_RELEASED = zc.ZoneService(
    heat=True, cool=False, cool_reason=zc.REASON_OUTDOOR_LOCKOUT, defended_heat=True, defended_cool=True
)

AC_PROMISES = (r"\bAC\b", "pre-cool", "Pre-cool", "HVAC Mode: Cool", "air condition")
HEAT_PROMISES = ("heater", "pre-heat", "HVAC Mode: Heat", "I'll warm", "warm things right up")


# ---------------------------------------------------------------------------------------------------------------
# The pure planning view
# ---------------------------------------------------------------------------------------------------------------


class TestZoneService:
    def test_full_service_leaves_every_mode_alone(self):
        for mode in ("heat", "cool", "off", "heat_cool"):
            assert zc.FULL_SERVICE.shown_mode(mode) == mode
        assert zc.FULL_SERVICE.unserved_reason() is None

    def test_single_family_runs_that_family_whatever_the_day_says(self):
        assert HEAT_ONLY.shown_mode("cool") == "heat"
        assert HEAT_ONLY.shown_mode("off") == "heat"
        assert COOL_ONLY.shown_mode("heat") == "cool"
        assert zc.ZoneService(heat=False, cool=False).shown_mode("heat") == "off"

    def test_serves_and_reason(self):
        assert HEAT_ONLY.serves("heat") and not HEAT_ONLY.serves("cool") and HEAT_ONLY.serves("off")
        assert COOL_LOCKED.unserved_reason() == ("cool", zc.REASON_OUTDOOR_LOCKOUT)
        assert COOL_ONLY.unserved_reason() == ("heat", zc.REASON_EQUIPMENT)

    def test_service_from_equipment_and_policy(self):
        z = zc.resolve_zone_capability(equipment_heat=True, equipment_cool=False)
        assert zc.service_from(z) == HEAT_ONLY
        z = zc.resolve_zone_capability(equipment_heat=True, equipment_cool=True, cool_lockout_engaged=True)
        assert zc.service_from(z) == COOL_LOCKED

    def test_a_safety_release_is_never_planned(self):
        """The cool lockout stepping aside past the tolerance runs cool, but nothing plans or promises it."""
        z = zc.resolve_zone_capability(
            equipment_heat=True,
            equipment_cool=True,
            cool_lockout_engaged=True,
            indoor_f=90.0,
            floor_f=64.0,
            ceiling_f=72.0,
            tolerance_f=2.0,
        )
        assert z.cool.available and z.cool.safety_released
        assert not zc.service_from(z).cool


# ---------------------------------------------------------------------------------------------------------------
# The engine's answer (the one source every surface reads)
# ---------------------------------------------------------------------------------------------------------------

NOW = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)


def _engine(modes, **config):
    engine, hass, scheduler, events = build_headless_engine(
        config={"comfort_heat": 70.0, "comfort_cool": 75.0, **config},
        climate_state=modes[1],
        climate_attributes={"current_temperature": 72.0, "hvac_modes": list(modes)},
        start_time=NOW,
    )
    return engine, scheduler


class TestEngineZoneService:
    def test_heat_only_equipment(self):
        engine, _ = _engine(("off", "heat"))
        assert engine.zone_service() == HEAT_ONLY

    def test_cool_only_equipment(self):
        engine, _ = _engine(("off", "cool"))
        assert engine.zone_service() == COOL_ONLY

    def test_cool_locked_by_outdoor(self):
        engine, scheduler = _engine(("off", "heat", "cool"), cool_lockout_outdoor_temp=60.8)
        with scheduler.installed():
            engine.update_outdoor_temp(44.6)
        assert engine.zone_service() == COOL_LOCKED

    def test_both(self):
        engine, _ = _engine(("off", "heat", "cool", "heat_cool"))
        assert engine.zone_service().full


# ---------------------------------------------------------------------------------------------------------------
# Day plan / operation timeline (#1542)
# ---------------------------------------------------------------------------------------------------------------


def _hot_snapshot(**over):
    now = datetime(2026, 11, 4, 8, 0, tzinfo=make_snapshot().now.tzinfo)
    snap = make_snapshot("hot", now=now)
    snap.pre_cool_trigger = now + timedelta(hours=18)
    snap.pre_cool_target = 70.0
    snap.tou_precondition_start = now + timedelta(hours=5)
    snap.tou_schedule_start = now + timedelta(hours=7)
    snap.tou_target = 71.0
    snap.tou_mode = "cool"
    snap.ceiling_breach = now + timedelta(hours=6)
    for k, v in over.items():
        setattr(snap, k, v)
    return snap


def _cold_snapshot(**over):
    snap = make_snapshot("cold")
    snap.tou_precondition_start = snap.now + timedelta(hours=5)
    snap.tou_schedule_start = snap.now + timedelta(hours=7)
    snap.tou_target = 70.0
    snap.tou_mode = "heat"
    for k, v in over.items():
        setattr(snap, k, v)
    return snap


def _modes_and_kinds(tl):
    return {s.mode for s in tl.steps if s.mode}, {s.kind for s in tl.steps}


class TestTimeline:
    def test_hot_day_heat_only_has_no_cool_step(self):
        tl = ot.build_timeline(_hot_snapshot(can_cool=False))
        modes, kinds = _modes_and_kinds(tl)
        assert "cool" not in modes
        assert ot.StepKind.PRECOOL_START not in kinds and ot.StepKind.CEILING_GUARD not in kinds
        assert all(d.get("hvac_mode") != "cool" for d in tl.days)
        assert not any(r.mode == "cool" for r in tl.runs)

    def test_hot_day_both_families_still_plans_cooling(self):
        """Control: the same snapshot with cooling plans the pre-cool, the bank and the ceiling guard."""
        tl = ot.build_timeline(_hot_snapshot())
        _modes, kinds = _modes_and_kinds(tl)
        assert ot.StepKind.PRECOOL_START in kinds and ot.StepKind.CEILING_GUARD in kinds

    def test_cold_day_cool_only_has_no_heat_step(self):
        tl = ot.build_timeline(_cold_snapshot(can_heat=False))
        modes, kinds = _modes_and_kinds(tl)
        assert "heat" not in modes and ot.StepKind.PREHEAT_START not in kinds
        assert all(d.get("hvac_mode") != "heat" for d in tl.days)

    def test_cold_day_control_plans_the_preheat(self):
        _modes, kinds = _modes_and_kinds(ot.build_timeline(_cold_snapshot()))
        assert ot.StepKind.PREHEAT_START in kinds

    def test_zone_masked_is_idempotent_and_leaves_full_zones_alone(self):
        snap = _hot_snapshot()
        assert ot.zone_masked(snap) is snap
        once = ot.zone_masked(_hot_snapshot(can_cool=False))
        assert ot.zone_masked(once) == once
        assert set(once.day_modes.values()) == {"heat"}
        assert once.pre_cool_trigger is None and once.tou_mode is None and once.ceiling_breach is None

    def test_away_widening_on_the_unserved_side_is_dropped(self):
        snap = _hot_snapshot(can_cool=False, occupancy_mode="away")
        snap.tou_av_start = snap.now + timedelta(hours=3)
        snap.tou_av_end = snap.now + timedelta(hours=6)
        snap.tou_av_expansion_f = 3.0
        assert ot.zone_masked(snap).tou_av_start is None
        heat_day = _cold_snapshot(can_cool=False, occupancy_mode="away")
        heat_day.tou_av_start = heat_day.now + timedelta(hours=3)
        heat_day.tou_av_end = heat_day.now + timedelta(hours=6)
        heat_day.tou_av_expansion_f = 3.0
        assert ot.zone_masked(heat_day).tou_av_start is not None  # the floor widening is still a heat-side step

    def test_latch_signature_sees_the_masked_mode(self):
        latch = ot.TimelineLatch()
        latch.update(_hot_snapshot(can_cool=False))
        assert latch._signature is not None and latch._signature.hvac_mode == "heat"


# ---------------------------------------------------------------------------------------------------------------
# Briefing (#1488)
# ---------------------------------------------------------------------------------------------------------------

_CFG = {
    "comfort_heat": 70.0,
    "comfort_cool": 75.0,
    "setback_heat": 60.0,
    "setback_cool": 80.0,
    "sleep_heat": 62.0,
    "sleep_cool": 68.0,
    "threshold_hot": 85,
}


def _c(day_type, high, low, **kw):
    return DayClassification(
        day_type=day_type,
        trend_direction=kw.get("trend", "warming"),
        trend_magnitude=kw.get("mag", 6.0),
        today_high=high,
        today_low=low,
        tomorrow_high=high + kw.get("tomorrow_delta", 6.0),
        tomorrow_low=low,
    )


def _brief(c, service, **kw):
    return str(
        generate_briefing(
            classification=c,
            comfort_heat=70.0,
            comfort_cool=75.0,
            setback_heat=60.0,
            setback_cool=80.0,
            wake_time=time(6, 30),
            sleep_time=time(22, 30),
            runtime_config=_CFG,
            zone_service=service,
            **kw,
        )
    )


@pytest.fixture(autouse=True)
def _frozen(frozen_now):
    frozen_now(datetime(2026, 7, 15, 6, 0, tzinfo=UTC))


class TestBriefing:
    def test_hot_day_heat_only_promises_no_ac(self):
        text = _brief(_c("hot", 92.0, 72.0), HEAT_ONLY)
        for phrase in AC_PROMISES:
            assert re.search(phrase, text) is None, phrase
        assert "There's no cooling equipment here" in text
        assert "blinds" in text
        assert "HVAC Mode: Heat at 70°F (no cooling)" in text

    def test_hot_day_control_still_promises_the_pre_cool(self):
        text = _brief(_c("hot", 92.0, 72.0), None)
        assert "pre-cool" in text and "HVAC Mode: Cool" in text

    def test_hot_day_cool_locked_names_the_lockout(self):
        text = _brief(_c("hot", 92.0, 72.0), COOL_LOCKED)
        assert "your outdoor lockout" in text
        assert "cooling locked out" in text
        for phrase in AC_PROMISES:
            assert re.search(phrase, text) is None, phrase

    def test_warm_day_heat_only_gives_advice_not_the_ac_handoff(self):
        text = _brief(_c("warm", 82.0, 58.0), HEAT_ONLY)
        assert re.search(r"\bAC\b", text) is None
        assert "draw the blinds" in text
        assert "at least 70°F" in text or "Heat at" in text

    def test_cold_day_cool_only_sun_is_your_heat(self):
        text = _brief(_c("cold", 38.0, 25.0, trend="cooling"), COOL_ONLY)
        for phrase in HEAT_PROMISES:
            assert phrase not in text, phrase
        assert "the sun is your heat" in text

    def test_cold_day_control_keeps_the_heater(self):
        assert "heater" in _brief(_c("cold", 38.0, 25.0), None)

    def test_mild_day_cool_only_no_heater_comes_back_on(self):
        c = _c("mild", 68.0, 48.0)
        c.windows_recommended = True
        text = _brief(c, COOL_ONLY)
        assert "heater" not in text

    def test_other_languages_render(self):
        text = str(
            generate_briefing(
                classification=_c("hot", 92.0, 72.0),
                comfort_heat=70.0,
                comfort_cool=75.0,
                setback_heat=60.0,
                setback_cool=80.0,
                wake_time=time(6, 30),
                sleep_time=time(22, 30),
                runtime_config=_CFG,
                zone_service=HEAT_ONLY,
                lang="de",
            )
        )
        assert "keine Kühlung" in text and "{" not in text


# ---------------------------------------------------------------------------------------------------------------
# Coordinator surfaces: chart, ODE curve, TOU window, pre-cool Status, incidents, Comfort Score, Next Action
# ---------------------------------------------------------------------------------------------------------------


def _coord(service: zc.ZoneService):
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.automation_engine = MagicMock()
    coord.automation_engine.zone_service = lambda: service
    coord.automation_engine._zone_bank_allowed = lambda fam: service.serves(fam)
    return coord


class TestCoordinatorSurfaces:
    def test_zone_service_falls_back_to_both(self):
        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        coord = object.__new__(ClimateAdvisorCoordinator)
        coord.automation_engine = MagicMock()  # answers a MagicMock, not a ZoneService
        assert coord.zone_service() is zc.FULL_SERVICE
        coord.automation_engine = None
        assert coord.zone_service() is zc.FULL_SERVICE

        def boom():
            raise RuntimeError

        coord.automation_engine = SimpleNamespace(zone_service=boom)
        assert coord.zone_service() is zc.FULL_SERVICE

    def test_tou_window_is_withheld_for_a_bank_the_zone_cannot_run(self):
        res = SimpleNamespace(schedule_id="s", precondition_start=NOW, schedule_start=NOW, target=71.0, mode="cool")
        coord = _coord(HEAT_ONLY)
        coord._tou_phase_resolution = res
        assert coord._tou_precondition_window_tuple() is None
        coord = _coord(zc.FULL_SERVICE)
        coord._tou_phase_resolution = res
        assert coord._tou_precondition_window_tuple() is not None

    def test_scheduled_pre_cool_line_is_hidden_without_cooling(self):
        from custom_components.climate_advisor.status_text import CodedText

        coord = _coord(COOL_LOCKED)
        coord._pre_cool_status = CodedText("scheduled", None, text="Pre-cool scheduled")
        assert coord._shown_pre_cool_status() is None
        coord._pre_cool_status = CodedText("active", None, text="pre-cool active")
        assert coord._shown_pre_cool_status() is not None  # what already happened stays
        coord = _coord(zc.FULL_SERVICE)
        coord._pre_cool_status = CodedText("scheduled", None, text="Pre-cool scheduled")
        assert coord._shown_pre_cool_status() is not None

    def test_unserved_edge_is_tolerated_for_incidents_and_score(self):
        coord = _coord(HEAT_ONLY)
        coord._is_nat_vent_tolerated_deviation = lambda *a: False
        coord._is_expected_control_deviation = lambda *a: False
        assert coord._is_tolerated_comfort_deviation(82.0, 70.0, 75.0) is True  # hot afternoon, no AC
        assert coord._is_tolerated_comfort_deviation(65.0, 70.0, 75.0) is False  # the heat edge is still judged
        coord = _coord(COOL_ONLY)
        coord._is_nat_vent_tolerated_deviation = lambda *a: False
        coord._is_expected_control_deviation = lambda *a: False
        assert coord._is_tolerated_comfort_deviation(65.0, 70.0, 75.0) is True
        assert coord._is_tolerated_comfort_deviation(82.0, 70.0, 75.0) is False
        coord = _coord(zc.FULL_SERVICE)
        coord._is_nat_vent_tolerated_deviation = lambda *a: False
        coord._is_expected_control_deviation = lambda *a: False
        assert coord._is_tolerated_comfort_deviation(82.0, 70.0, 75.0) is False

    def test_band_advisory_and_defense_lines(self):
        from custom_components.climate_advisor.coordinator import _compute_defense_lines

        assert _coord(COOL_LOCKED)._band_advisory() == {"family": "cool", "reason": "outdoor_lockout"}
        assert _coord(COOL_ONLY)._band_advisory() == {"family": "heat", "reason": "equipment"}
        assert _coord(zc.FULL_SERVICE)._band_advisory() is None
        band = [{"ts": "t", "lower": 70.0, "upper": 75.0}]
        assert _compute_defense_lines(band, HEAT_ONLY) == [{"ts": "t", "heat": 70.0, "cool": None}]
        assert _compute_defense_lines(band) == [{"ts": "t", "heat": 70.0, "cool": 75.0}]

    def test_predicted_curve_does_not_cool_a_heat_only_zone(self):
        from unittest.mock import patch

        from custom_components.climate_advisor.coordinator import _build_predicted_indoor_future

        now = datetime(2026, 7, 15, 8, 0)
        forecast = [{"datetime": (now + timedelta(hours=h)).isoformat(), "temperature": 95.0} for h in range(1, 13)]
        model = {"confidence": "high", "k_passive": -0.2, "k_active_heat": 2.0, "k_active_cool": -3.0}
        band = [{"ts": (now + timedelta(hours=h)).isoformat(), "lower": 70.0, "upper": 75.0} for h in range(1, 13)]
        kw = dict(current_indoor_temp=74.0, thermal_model=model, classification=_c("hot", 95.0, 75.0))
        with patch("custom_components.climate_advisor.coordinator.dt_util.as_local", side_effect=lambda x: x):
            full = _build_predicted_indoor_future(forecast, dict(_CFG), now, band_schedule=band, **kw)
            heat_only = _build_predicted_indoor_future(
                forecast, dict(_CFG), now, band_schedule=band, zone_service=HEAT_ONLY, **kw
            )
        # with the AC the curve is held back; with nothing to cool it the house drifts toward the 95 F outdoor air
        assert max(p["temp"] for p in heat_only) > max(p["temp"] for p in full) + 5.0

    @pytest.mark.parametrize("day_type", ["cold", "mild"])
    def test_next_action_open_window_cool_only_keeps_the_warmth(self, day_type):
        """No heater to enable in a cool-only zone: closing up keeps the warmth (mirror of "help the heater")."""
        from unittest.mock import patch

        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        c = _c(day_type, 40.0 if day_type == "cold" else 66.0, 30.0 if day_type == "cold" else 50.0)
        texts = {}
        for name, service in (("cool_only", COOL_ONLY), ("both", zc.FULL_SERVICE)):
            coord = _coord(service)
            coord.config = {
                "comfort_heat": 68.0,
                "comfort_cool": 74.0,
                "temp_unit": "fahrenheit",
                "fan_mode": "disabled",
            }
            coord._occupancy_mode = "home"
            coord.hass = MagicMock()
            coord._thermostat_hvac_mode = lambda: "cool"
            coord._learned_swings = lambda: (None, None)
            with patch(
                "custom_components.climate_advisor.coordinator.dt_util.now", return_value=datetime(2026, 1, 5, 9)
            ):
                texts[name] = ClimateAdvisorCoordinator._compute_next_action(
                    coord, c, indoor_temp=62.0, outdoor_temp=35.0, windows_physically_open=True, ae=None
                )
        assert "heater" not in str(texts["cool_only"]) and "warmth" in str(texts["cool_only"])
        assert "heater" in str(texts["both"])


# ---------------------------------------------------------------------------------------------------------------
# Promises (P7 comfort_kept) and the AI context
# ---------------------------------------------------------------------------------------------------------------


class TestPromisesAndAi:
    def _hot_run(self, serves_cool):
        from custom_components.climate_advisor.invariant_watchdog import PromiseConfig, check_p7_comfort_kept
        from custom_components.climate_advisor.promise_journal import PromiseJournal, Reading

        j = PromiseJournal()
        for m in range(0, 120, 5):
            j.record_reading(
                Reading(
                    ts=NOW + timedelta(minutes=m),
                    hvac_mode="heat",
                    hvac_action="idle",
                    indoor_f=80.0,
                    fan_on=False,
                    contacts_open=False,
                    occupancy="home",
                    serves_heat=True,
                    serves_cool=serves_cool,
                )
            )
        cfg = PromiseConfig(comfort_heat=70.0, comfort_cool=75.0, sleep_heat=70.0, sleep_cool=75.0)
        return check_p7_comfort_kept(j.window(NOW + timedelta(minutes=120)), cfg)

    def test_p7_never_judges_the_unserved_edge(self):
        assert self._hot_run(serves_cool=False) == []

    def test_p7_control_still_fires_when_cooling_is_served(self):
        assert self._hot_run(serves_cool=True)

    def test_ai_context_names_the_advisory_side(self):
        from custom_components.climate_advisor.ai_skills_context import _zone_service_line, _zone_unserved_reason

        coord = SimpleNamespace(zone_service=lambda: COOL_LOCKED)
        assert _zone_unserved_reason(coord, "cool") == "outdoor_lockout"
        assert _zone_unserved_reason(coord, "heat") is None
        assert "cool=advisory (outdoor_lockout)" in _zone_service_line(coord)
        assert _zone_service_line(SimpleNamespace(zone_service=lambda: zc.FULL_SERVICE)) is None
        assert _zone_service_line(SimpleNamespace()) is None


# ---------------------------------------------------------------------------------------------------------------
# The #1544 matrix: every golden through the real engine as [off, heat], [off, cool] and season Heat only
# ---------------------------------------------------------------------------------------------------------------

_GOLDEN = sorted(
    p for p in (REPO_ROOT / "tools" / "simulations" / "golden").glob("*.json") if p.name != "MANIFEST.json"
)


@pytest.mark.parametrize("scenario_path", _GOLDEN, ids=lambda p: p.stem)
def test_matrix_no_surface_names_a_missing_mode(scenario_path):
    """``tools/zone_matrix.py`` for one golden: commands, plan and briefing never name the family the device lacks.

    The run must have judged something: a plan for every coordinator scenario with a classification, a briefing (full
    and TLDR) for every scenario with one. Positive check (review finding 1): a window period the heat-and-cool
    home is offered for free cooling is still offered to the heat-only home, and free heating to the cool-only one.
    """
    from tools.zone_matrix import ALL_DEVICES, BOTH, run_one

    both = run_one(scenario_path, BOTH)
    assert both.error is None, f"{scenario_path.stem} [both]: {both.error}"
    runs = {device: run_one(scenario_path, device) for device in ALL_DEVICES}
    for device, r in runs.items():
        assert r.error is None, f"{scenario_path.stem} [{device}]: {r.error}"
        assert not r.problems, f"{scenario_path.stem} [{device}]:\n" + "\n".join(r.problems)
        if r.not_locked:
            assert device == "cool_lockout", device  # only the outdoor lockout depends on a reading arriving
            continue
        assert r.briefing_checked is r.has_classification, (device, r)
        assert r.plan_checked is (r.has_coordinator and r.has_classification), (device, r)
    if "free_cooling" in both.window_kinds:
        assert "free_cooling" in runs["heat_only"].window_kinds, runs["heat_only"].window_kinds
    if "free_heating" in both.window_kinds:
        assert "free_heating" in runs["cool_only"].window_kinds, runs["cool_only"].window_kinds


def test_matrix_judged_something():
    """The matrix as a whole: the lockout device engaged in most scenarios and plans were built."""
    from tools.zone_matrix import run_one

    sample = [p for p in _GOLDEN if p.stem.startswith(("hot", "warm", "cold"))][:6] or _GOLDEN[:6]
    results = [run_one(p, "cool_lockout") for p in sample]
    assert sum(not r.not_locked for r in results) >= 1


def test_matrix_detects_a_cool_command_on_a_heat_only_device():
    """Effectiveness: the matrix's own checks flag each kind of violation."""
    from tools.zone_matrix import _briefing_problems, _command_problems, _plan_problems

    log = [{"domain": "climate", "service": "set_hvac_mode", "data": {"hvac_mode": "cool"}, "ts": NOW}]
    assert _command_problems(log, "heat_only")
    assert not _command_problems(log, "cool_only")
    tl = ot.build_timeline(_hot_snapshot())
    assert _plan_problems(tl, "heat_only")
    assert _briefing_problems("I'll handle the AC transition", "heat_only")
    assert _briefing_problems("the heater comes back on", "cool_only")


# ---------------------------------------------------------------------------------------------------------------
# The real coordinator (headless): what each surface is fed
# ---------------------------------------------------------------------------------------------------------------


def _headless(modes, **config):
    from tools.sim_harness.build_coordinator import build_headless_coordinator

    coord, hass, scheduler, events = build_headless_coordinator(
        config={"comfort_heat": 70.0, "comfort_cool": 75.0, "sleep_heat": 62.0, "sleep_cool": 68.0, **config},
        climate_state=modes[1],
        climate_attributes={"current_temperature": 72.0, "hvac_modes": list(modes), "temperature": 70.0},
        start_time=NOW,
        skip_startup_coalesce=True,
    )
    return coord, scheduler


def _hot_classification():
    c = _c("hot", 95.0, 72.0, trend="warming", mag=8.0)
    assert c.hvac_mode == "cool"
    return c


class TestHeadlessCoordinator:
    def test_timeline_snapshot_carries_the_served_families(self):
        for modes, heat, cool in ((("off", "heat"), True, False), (("off", "cool"), False, True)):
            coord, scheduler = _headless(modes)
            with scheduler.installed():
                coord._current_classification = _hot_classification()
                snap = coord._timeline_snapshot(coord._current_classification)
            assert (snap.can_heat, snap.can_cool) == (heat, cool)

    def test_timeline_snapshot_policy_lock(self):
        coord, scheduler = _headless(("off", "heat", "cool"), hvac_season="heat_only")
        with scheduler.installed():
            snap = coord._timeline_snapshot(_hot_classification())
        assert snap.can_heat and not snap.can_cool

    def test_night_plan_is_told_the_served_families(self):
        from unittest.mock import patch

        from custom_components.climate_advisor import v4_shadow

        coord, scheduler = _headless(("off", "heat"))
        captured = {}

        def _capture(**kw):
            captured.update(kw)
            raise RuntimeError("captured")

        with scheduler.installed():
            snap = coord._timeline_snapshot(_hot_classification())
            coord._v4_last_future = {"corrected": [(NOW, 90.0)], "predictions": None}
            coord._chart_log = MagicMock()
            coord._chart_log.last_entry.return_value = {}
            coord._timeline_latch = MagicMock(timeline=None)
            with patch.object(v4_shadow, "night_inputs", side_effect=_capture), pytest.raises(RuntimeError):
                coord._timeline_night_plan(snap, NOW, NOW + timedelta(hours=8), MagicMock(), MagicMock(), {}, None)
        assert captured["can_heat"] is True and captured["can_cool"] is False

    def test_daily_record_names_the_mode_the_zone_runs(self):
        coord, scheduler = _headless(("off", "heat"))
        with scheduler.installed():
            coord._today_record = None
            coord._ensure_today_record(_hot_classification())
        assert coord._today_record.hvac_mode_recommended == "heat"

    def test_promise_reading_carries_the_served_families(self):
        coord, scheduler = _headless(("off", "heat"))
        with scheduler.installed():
            r = coord._promise_reading_now(NOW)
        assert r.serves_heat is True and r.serves_cool is False

    def test_pre_cool_is_not_scheduled_without_cooling(self):
        for modes, scheduled in ((("off", "heat"), False), (("off", "heat", "cool"), True)):
            coord, scheduler = _headless(modes)
            with scheduler.installed():
                coord._current_classification = _hot_classification()
                coord._pre_cool_trigger_scheduled = False
                coord._maybe_schedule_pre_cool()
            assert bool(coord._pre_cool_trigger_scheduled) is scheduled, modes
            if not scheduled:
                assert coord._pre_cool_status is None and coord._pre_cool_trigger_dt is None

    def test_pre_cool_skipped_for_cooling_clears_the_status_line(self):
        from unittest.mock import AsyncMock, patch

        from custom_components.climate_advisor.status_text import CodedText

        coord, scheduler = _headless(("off", "heat", "cool"), hvac_season="heat_only")
        with scheduler.installed():
            coord._current_classification = _hot_classification()
            coord._pre_cool_status = CodedText("scheduled", None, text="Pre-cool scheduled")
            coord._pre_cool_modifier_for_night = lambda _t: -2.0
            coord.async_refresh = AsyncMock()
            skipped = CodedText("skipped_cool_unavailable", None, text="skipped: cooling unavailable")
            with patch("custom_components.climate_advisor.coordinator.decide", AsyncMock(return_value=skipped)):
                from tools.sim_harness._loop import run_coro

                run_coro(coord._async_pre_cool_trigger(NOW))
        assert coord._pre_cool_status is None

    def test_chart_band_draws_no_pre_cool_dip_without_cooling(self):
        results = {}
        for modes in (("off", "heat"), ("off", "heat", "cool")):
            coord, scheduler = _headless(modes, sleep_cool=76.0)
            with scheduler.installed():
                coord._current_classification = _hot_classification()
                coord._hourly_forecast_temps = [
                    {"datetime": (NOW + timedelta(hours=h)).isoformat(), "temperature": 90.0} for h in range(1, 30)
                ]
                coord._compute_pre_cool_trigger_time = lambda: NOW + timedelta(hours=18)
                coord._pre_cool_modifier_for_night = lambda _t: -2.0
                band = coord._build_target_band_for(NOW, None)
            results[modes] = min(b["upper"] for b in band)
        assert results[("off", "heat")] > results[("off", "heat", "cool")]

    def test_briefing_bedtime_is_the_sleep_floor_on_a_heat_only_hot_day(self):
        coord, scheduler = _headless(("off", "heat"))
        with scheduler.installed():
            coord._current_classification = _hot_classification()
            full, _short = coord._build_briefing_text(coord._current_classification, suggestions=[])
        assert "Bedtime Setback: 62°F" in full and "HVAC Mode: Heat" in full
        assert re.search(r"\bAC\b", full) is None

    def test_status_never_says_pre_cooling_for_a_suppressed_bank(self):
        from custom_components.climate_advisor.scheduler import TOUPhase

        coord, scheduler = _headless(("off", "heat"))
        with scheduler.installed():
            coord._current_classification = _hot_classification()
            coord._tou_phase_resolution = SimpleNamespace(
                phase=TOUPhase.PRECONDITIONING,
                mode="cool",
                target=70.0,
                schedule_id="s",
                precondition_start=NOW - timedelta(minutes=10),
                schedule_start=NOW + timedelta(hours=1),
            )
            coord.current_hold = lambda *_a, **_k: None  # the short fallback wording names the bank's family
            text = str(coord._compute_automation_status())
        assert "pre-cool" not in text.lower(), text

    def test_degraded_next_automation_names_no_cooling(self):
        coord, scheduler = _headless(("off", "heat"))
        with scheduler.installed():
            c = _hot_classification()
            coord._current_classification = c
            coord._surface_timeline = lambda **_kw: None
            coord._pre_cool_trigger_dt = NOW + timedelta(hours=3)
            coord._pre_cool_target = 66.0
            action, _when = coord._compute_next_automation_action(c)
        assert "cool" not in str(action).lower(), action


class TestAiContextBuilders:
    def _coord(self, service):
        coord = MagicMock()
        coord.data = {"hvac_mode": "heat", "hvac_action": "idle"}
        coord.config = {"climate_entity": "climate.x", "comfort_heat": 70.0, "comfort_cool": 75.0}
        coord.zone_service = lambda: service
        coord.learning = None
        return coord

    def _hass(self, indoor):
        hass = MagicMock()
        hass.states.get.return_value = SimpleNamespace(state="heat", attributes={"current_temperature": indoor})
        return hass

    def test_hvac_entity_section_names_the_advisory_side(self):
        from custom_components.climate_advisor.ai_skills_context import build_hvac_entity_context
        from tools.sim_harness._loop import run_coro

        text = run_coro(build_hvac_entity_context(self._hass(72.0), self._coord(HEAT_ONLY)))
        assert "zone_service:" in text and "cool=advisory (equipment)" in text
        assert "zone_service:" not in run_coro(
            build_hvac_entity_context(self._hass(72.0), self._coord(zc.FULL_SERVICE))
        )

    def test_cross_validation_does_not_flag_the_unserved_edge(self):
        from custom_components.climate_advisor.ai_skills_context import build_state_cross_validation_context
        from tools.sim_harness._loop import run_coro

        hot = run_coro(build_state_cross_validation_context(self._hass(82.0), self._coord(HEAT_ONLY)))
        assert "[FLAG]" not in hot and "advisory edge" in hot
        control = run_coro(build_state_cross_validation_context(self._hass(82.0), self._coord(zc.FULL_SERVICE)))
        assert "[FLAG]" in control


def test_chart_activity_never_shows_cooling_on_a_heat_only_zone():
    """The chart's forward activity (and effective target) never draws an AC hour the zone cannot run."""
    coord, scheduler = _headless(("off", "heat"))
    with scheduler.installed():
        coord._current_classification = _hot_classification()
        coord._hourly_forecast_temps = [
            {"datetime": (NOW + timedelta(hours=h)).isoformat(), "temperature": 98.0} for h in range(1, 30)
        ]
        coord._thermal_model = {"confidence": "high", "k_passive": -0.2, "k_active_heat": 2.0, "k_active_cool": -3.0}
        data = coord.get_chart_data("24h")
    modes = {e.get("hvac_mode") for e in data.get("predicted_activity") or []}
    assert data["predicted_activity"], "no forward activity to judge"
    assert "cool" not in modes
    assert data["band_advisory"] == {"family": "cool", "reason": "equipment"}


def test_briefing_bedtime_uses_the_learned_setback_for_the_side_the_zone_holds():
    """A heat-only zone on a hot day holds heat at bedtime: the learned heat-side set-back is the number quoted."""
    from unittest.mock import patch

    coord, scheduler = _headless(("off", "heat"))
    with scheduler.installed():
        coord._current_classification = _hot_classification()

        def _setback(_config, _model, c):
            return 64.0 if c.hvac_mode == "heat" else 71.0

        with patch("custom_components.climate_advisor.coordinator.compute_bedtime_setback", side_effect=_setback):
            full, _short = coord._build_briefing_text(coord._current_classification, suggestions=[])
    assert "Bedtime Setback: 64°F" in full, full


def test_briefing_zone_with_neither_family_says_off():
    """Cool-only equipment with the cool lockout engaged: nothing can run, the row says so (never "Cool at")."""
    neither = zc.ZoneService(heat=False, cool=False, heat_reason="equipment", cool_reason="outdoor_lockout")
    text = _brief(_c("hot", 92.0, 72.0), neither)
    assert "HVAC Mode: Off (no heating or cooling available)" in text
    assert re.search(r"\bAC\b", text) is None


def test_ai_context_unreadable_zone_counts_as_both():
    from custom_components.climate_advisor.ai_skills_context import _zone_unserved_reason

    def boom():
        raise RuntimeError

    assert _zone_unserved_reason(SimpleNamespace(zone_service=boom), "cool") is None


# ---------------------------------------------------------------------------------------------------------------
# Review fixes (#1544 phase 2 review)
# ---------------------------------------------------------------------------------------------------------------


def _released_engine():
    """A heat-and-cool home with the season Heat only whose cool lock is stepping aside for the safety release."""
    engine, scheduler = _engine(("off", "heat", "cool"), hvac_season="heat_only")
    engine._zone_safety_released = {"heat": False, "cool": True}
    return engine, scheduler


class TestReviewWindows:
    """Finding 1: the unserved side's advice IS the window period; a heat-only hot evening still gets one."""

    def _evening(self, **over):
        from tests.test_operation_timeline import _evening_cooling

        snap = _evening_cooling()
        for k, v in over.items():
            setattr(snap, k, v)
        return snap

    def test_heat_only_hot_evening_still_opens_the_windows(self):
        tl = ot.build_timeline(self._evening(can_cool=False))
        accepted = [c for c in tl.candidates if c.accepted]
        assert any(c.kind == "free_cooling" for c in accepted), [(c.kind, c.accepted) for c in tl.candidates]
        assert any(s.kind == ot.StepKind.WINDOWS_OPEN for s in tl.steps)
        assert all(s.mode != "cool" for s in tl.steps)

    def test_cool_only_cool_day_still_gets_free_heating(self):
        now = make_snapshot("mild").now.replace(hour=9, minute=30)
        snap = make_snapshot("mild", now=now, indoor=65.0, thermal_model=None)
        snap.thermal_model = dict(snap.thermal_model or {}) or None
        from tests.timeline_fixtures import MODEL

        snap.thermal_model = dict(MODEL, k_vent_window=-0.5)
        snap.hourly = [(ts, 80.0 if 9 <= ts.hour < 17 else 50.0) for ts, _v in snap.hourly]
        snap.can_heat = False
        tl = ot.build_timeline(snap)
        kinds = {c.kind for c in tl.candidates if c.accepted}
        assert "free_heating" in kinds, [
            (c.kind, c.accepted, c.reason if hasattr(c, "reason") else None) for c in tl.candidates
        ]


class TestReviewJudging:
    """Findings 2 and 3: judge what can run NOW (the safety release included); plan only what is served."""

    def test_released_side_is_judged_not_served(self):
        engine, _ = _released_engine()
        svc = engine.zone_service()
        assert not svc.serves("cool") and svc.defends("cool") and svc.released("cool")
        assert not svc.released("heat")

    def test_incidents_and_score_judge_a_released_side(self):
        coord = _coord(COOL_RELEASED)
        coord._is_nat_vent_tolerated_deviation = lambda *a: False
        coord._is_expected_control_deviation = lambda *a: False
        assert coord._is_tolerated_comfort_deviation(82.0, 70.0, 75.0) is False  # the release runs and fails: judged
        coord = _coord(COOL_LOCKED)
        coord._is_nat_vent_tolerated_deviation = lambda *a: False
        coord._is_expected_control_deviation = lambda *a: False
        assert coord._is_tolerated_comfort_deviation(82.0, 70.0, 75.0) is True

    def test_p7_reading_judges_a_released_side(self):
        coord, scheduler = _headless(("off", "heat", "cool"), hvac_season="heat_only")
        with scheduler.installed():
            coord.automation_engine._zone_safety_released = {"heat": False, "cool": True}
            r = coord._promise_reading_now(NOW)
        assert r.serves_cool is True

    def test_ai_cross_validation_flags_a_released_side(self):
        from custom_components.climate_advisor.ai_skills_context import build_state_cross_validation_context
        from tools.sim_harness._loop import run_coro

        helper = TestAiContextBuilders()
        text = run_coro(build_state_cross_validation_context(helper._hass(82.0), helper._coord(COOL_RELEASED)))
        assert "[FLAG]" in text

    def test_stall_decision_excludes_an_undefended_direction(self):
        from custom_components.climate_advisor.comfort_stall_decision import StallReason, decide_comfort_stall

        kw = dict(
            indoor=64.0,
            comfort_heat=70.0,
            comfort_cool=75.0,
            now=NOW,
            heat_margin_f=1.0,
            cool_margin_f=1.0,
            sustain_seconds=0,
            previous_direction=None,
            previous_since=None,
            last_active={},
            thermostat_available=True,
            has_hvac_action=True,
            hvac_modes=["off", "heat", "cool"],
            manual_control=False,
            sensor_open=False,
            startup_coalescing=False,
            nat_vent_tolerated=False,
        )
        d = decide_comfort_stall(**kw, defended=lambda fam: fam != "heat")
        assert d.reason is StallReason.EXCLUDED and d.excluded_by == "zone_not_defended"
        assert decide_comfort_stall(**kw).direction == "heat"
        assert decide_comfort_stall(**kw, defended=lambda fam: True).direction == "heat"

    def test_coordinator_stall_check_is_told_the_season(self):
        from unittest.mock import patch

        from custom_components.climate_advisor.comfort_stall_decision import decide_comfort_stall

        coord, scheduler = _headless(("off", "heat", "cool"), hvac_season="cool_only")
        captured = {}

        def _spy(**kw):
            captured.update(kw)
            return decide_comfort_stall(**kw)

        with scheduler.installed(), patch("custom_components.climate_advisor.coordinator.decide_comfort_stall", _spy):
            coord._update_comfort_stall(64.0, 70.0, 75.0, NOW)
        assert captured["defended"]("heat") is False and captured["defended"]("cool") is True
        assert coord._stall_status is None


class TestReviewCurrentHour:
    """Finding 3: the current hour shows what runs now (a released side), later hours stay unserved."""

    def test_plan_simulates_a_released_side_only_in_the_current_hour(self):
        def snap(**kw):
            base = make_snapshot("hot").now
            sn = make_snapshot("hot", now=base.replace(hour=13), indoor=80.0)
            for k, v in kw.items():
                setattr(sn, k, v)
            return sn

        released = snap(can_cool=False, released_now=("cool",))
        cool_runs = [r for r in ot.build_timeline(released).runs if r.mode == "cool"]
        assert cool_runs, "the released cool runs now"
        assert all(r.start < released.now + timedelta(hours=1) for r in cool_runs)
        assert not [r for r in ot.build_timeline(snap(can_cool=False)).runs if r.mode == "cool"]

    def test_coordinator_passes_the_released_side(self):
        coord, scheduler = _headless(("off", "heat", "cool"), hvac_season="heat_only")
        with scheduler.installed():
            coord.automation_engine._zone_safety_released = {"heat": False, "cool": True}
            snap = coord._timeline_snapshot(_hot_classification())
        assert snap.released_now == ("cool",) and not snap.can_cool

    def test_chart_current_hour_shows_the_released_side_running(self):
        coord, scheduler = _headless(("off", "heat", "cool"), hvac_season="heat_only")
        with scheduler.installed():
            coord.automation_engine._zone_safety_released = {"heat": False, "cool": True}
            coord._current_classification = _hot_classification()
            coord._hourly_forecast_temps = [
                {"datetime": (NOW + timedelta(minutes=30 + 60 * h)).isoformat(), "temperature": 98.0}
                for h in range(0, 30)
            ]
            coord._thermal_model = {
                "confidence": "high",
                "k_passive": -0.2,
                "k_active_heat": 2.0,
                "k_active_cool": -3.0,
            }
            data = coord.get_chart_data("24h")
        rows = [(dt_util_parse(e["ts"]), e.get("hvac_mode")) for e in data["predicted_activity"]]
        now_rows = [m for t, m in rows if t is not None and t <= NOW + timedelta(hours=1)]
        later = [m for t, m in rows if t is not None and t > NOW + timedelta(hours=1)]
        assert "cool" in now_rows, rows[:4]
        assert later and "cool" not in later


def dt_util_parse(ts):
    from datetime import datetime as _dt

    try:
        v = _dt.fromisoformat(ts)
    except TypeError, ValueError:
        return None
    return v if v.tzinfo else v.replace(tzinfo=UTC)


class TestReviewDayLevel:
    """Finding 4: a morning outdoor lockout the forecast releases today does not take the side away all day."""

    def _desert(self, high):
        coord, scheduler = _headless(("off", "heat", "cool"), cool_lockout_outdoor_temp=60.0)
        with scheduler.installed():
            coord.automation_engine.update_outdoor_temp(57.0)
            coord._hourly_forecast_temps = [
                {"datetime": (NOW + timedelta(hours=h)).isoformat(), "temperature": min(high, 57.0 + 5 * h)}
                for h in range(0, 14)
            ]
            assert not coord.zone_service().cool  # locked at dawn
            return coord, scheduler, coord.day_zone_service(NOW)

    def test_forecast_release_today_counts_as_served(self):
        _coord_, _s, day = self._desert(98.0)
        assert day.cool and day.cool_reason is None

    def test_forecast_that_never_releases_keeps_the_lock(self):
        _coord_, _s, day = self._desert(62.0)  # 62 < 60 + 3.6: the hysteresis never releases
        assert not day.cool and day.cool_reason == zc.REASON_OUTDOOR_LOCKOUT

    def test_season_is_not_projected(self):
        coord, scheduler = _headless(("off", "heat", "cool"), hvac_season="heat_only")
        with scheduler.installed():
            coord._hourly_forecast_temps = [
                {"datetime": (NOW + timedelta(hours=h)).isoformat(), "temperature": 98.0} for h in range(0, 10)
            ]
            assert not coord.day_zone_service(NOW).cool

    def test_desert_briefing_does_not_say_no_cooling(self):
        coord, scheduler, _day = self._desert(98.0)
        with scheduler.installed():
            coord._current_classification = _hot_classification()
            full, _short = coord._build_briefing_text(coord._current_classification, suggestions=[])
            coord._today_record = None
            coord._ensure_today_record(coord._current_classification)
        assert "Cooling is off" not in full and "no cooling" not in full
        assert coord._today_record.hvac_mode_recommended == "cool"


class TestReviewSignatureAndHistory:
    def test_served_families_change_regenerates_the_plan(self):
        snap = _cold_snapshot()
        old = ot.PlanSignature.of(snap, ot.build_segments(snap, ot.timeline_end(snap)))
        snap2 = _cold_snapshot(can_cool=False)
        new = ot.PlanSignature.of(snap2, ot.build_segments(snap2, ot.timeline_end(snap2)))
        assert old.hvac_mode == new.hvac_mode == "heat"  # the day mode is unchanged ...
        assert "zone" in ot.material_changes(old, new, snap.now)  # ... and the plan still regenerates

    def test_historical_chart_has_no_band_advisory(self):
        coord, scheduler = _headless(("off", "heat"))
        with scheduler.installed():
            coord._current_classification = _hot_classification()
            data = coord.get_chart_data("24h", before_ts=(NOW - timedelta(days=2)).timestamp())
        assert data["band_advisory"] is None


@pytest.mark.parametrize("modes", [("off", "heat"), ("off", "heat", "cool")])
def test_ode_guard_logs_no_breach_for_a_zone_that_cannot_cool(caplog, modes):
    """Finding 8: the zone's predicted curve is honest drift, so it predicts breaches the guard can never act on; the
    guard asks the zone first, so a heat-only zone gets no "breach predicted" INFO line every cycle (control: a zone
    that can cool still logs it)."""
    import asyncio
    import logging

    from custom_components.climate_advisor.ode_ceiling_guard import OdeCeilingGuardOutcome
    from tests.test_zone_capability_1699 import _classification

    engine, scheduler = _engine(modes)
    engine._last_comfort_band = None
    decision = SimpleNamespace(
        ceiling_decision=SimpleNamespace(
            outcome=OdeCeilingGuardOutcome.STANDING_BY,
            ceiling=75.0,
            outdoor=95.0,
            indoor=74.0,
            breach_ts=NOW + timedelta(hours=3),
            hours_to_breach=3.0,
            lead_min=30.0,
        )
    )
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor.automation")
    with scheduler.installed():
        asyncio.run(engine._apply_ode_ceiling_guard_decision(_classification("warm", "off"), [], decision))
    logged = any("breach predicted" in r.getMessage() and r.levelno >= logging.INFO for r in caplog.records)
    assert logged is (modes != ("off", "heat"))
