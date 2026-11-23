"""Issue #1699 phase 1: what this zone can do right now (outdoor lockouts, season setting, equipment).

Occupant view: on a near-freezing night with a cool lockout set, Climate Advisor never switches the thermostat to cool
because the house sits in the cooling margin of the sleep ceiling (#1560, Nov 12); heating stays armed. A locked side
behaves exactly like a heat-only or cool-only thermostat, and a house that goes deeper past the locked side's edge than
the day's settling tolerance still gets that side (the safety release).

Three layers: the pure resolver and hysteresis (every row of the #1699 state table), the selector leaf / FSM, and the
real ``AutomationEngine`` through the production harness (FakeHass + virtual clock), including the coordinator.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.climate_advisor import zone_capability as zc
from custom_components.climate_advisor.automation import HvacCommandResult, hvac_outcome_label
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.comfort_family_decision import ComfortFamilyInputs, decide_comfort_family
from custom_components.climate_advisor.comfort_family_fsm import (
    ComfortFamilyDwellState,
    ComfortFamilyEvent,
    ComfortFamilyEventKind,
    ComfortFamilyFsmInputs,
    ComfortFamilyState,
    transition,
)
from tools.sim_harness.build_engine import build_headless_engine
from tools.sim_harness.run_production import run_production_scenario

NOW = datetime(2026, 11, 12, 0, 20, tzinfo=UTC)
ENTITY = "climate.test_thermostat"


def C(c: float) -> float:
    return round(c * 9 / 5 + 32, 2)


# ---------------------------------------------------------------------------------------------------------------
# Pure hysteresis
# ---------------------------------------------------------------------------------------------------------------


class TestCoolLockoutHysteresis:
    def test_engages_strictly_below_the_threshold(self):
        assert zc.step_cool_lockout(False, 60.7, 60.8) is True
        assert zc.step_cool_lockout(False, 60.8, 60.8) is False

    def test_releases_at_threshold_plus_two_celsius(self):
        assert zc.step_cool_lockout(True, 60.8 + 3.5, 60.8) is True  # still inside the hysteresis
        assert zc.step_cool_lockout(True, 60.8 + 3.6, 60.8) is False

    def test_missing_reading_keeps_the_previous_state(self):
        assert zc.step_cool_lockout(True, None, 60.8) is True
        assert zc.step_cool_lockout(False, None, 60.8) is False

    def test_no_threshold_is_never_engaged(self):
        assert zc.step_cool_lockout(True, 10.0, None) is False


class TestHeatLockoutHysteresis:
    def test_engages_strictly_above_the_threshold(self):
        assert zc.step_heat_lockout(False, 65.1, 65.0) is True
        assert zc.step_heat_lockout(False, 65.0, 65.0) is False

    def test_releases_at_threshold_minus_two_celsius(self):
        assert zc.step_heat_lockout(True, 65.0 - 3.5, 65.0) is True
        assert zc.step_heat_lockout(True, 65.0 - 3.6, 65.0) is False

    def test_missing_reading_keeps_the_previous_state(self):
        assert zc.step_heat_lockout(True, None, 65.0) is True

    def test_no_threshold_is_never_engaged(self):
        assert zc.step_heat_lockout(True, 99.0, None) is False


def test_overlapping_thresholds_are_rejected():
    """The heat lockout must be at least the 2 °C hysteresis above the cool lockout, or both can be engaged at once."""
    assert zc.lockout_thresholds_overlap(60.0, 60.0) is True
    assert zc.lockout_thresholds_overlap(60.0, 55.0) is True
    assert zc.lockout_thresholds_overlap(C(16.0), C(17.0)) is True  # both engaged between 17 and 18 °C
    assert zc.lockout_thresholds_overlap(C(16.0), C(18.0)) is False
    assert zc.lockout_thresholds_overlap(60.0, 63.6) is False
    assert zc.lockout_thresholds_overlap(None, 40.0) is False
    assert zc.lockout_thresholds_overlap(60.0, None) is False


# ---------------------------------------------------------------------------------------------------------------
# Pure resolver: the #1699 state table
# ---------------------------------------------------------------------------------------------------------------

BAND = {"floor_f": 64.4, "ceiling_f": 71.6, "tolerance_f": 5.0}


def _zone(**kw):
    base = {"equipment_heat": True, "equipment_cool": True, **BAND}
    base.update(kw)
    return zc.resolve_zone_capability(**base)


class TestStateTable:
    def test_both_available_today(self):
        z = _zone(indoor_f=70.7)
        assert z.heat.available and z.cool.available
        assert z.mode_allowed("heat_cool")

    def test_heat_only_equipment_cool_never_released(self):
        z = _zone(equipment_cool=False, indoor_f=90.0)  # 18.4 F past the ceiling: still nothing to run
        assert z.heat.available
        assert not z.cool.available and z.cool.reason == zc.REASON_EQUIPMENT and not z.cool.safety_released

    def test_cool_only_equipment(self):
        z = _zone(equipment_heat=False, indoor_f=50.0)
        assert z.cool.available and not z.heat.available and z.heat.reason == zc.REASON_EQUIPMENT

    def test_cool_locked_out_inside_the_band(self):
        z = _zone(cool_lockout_engaged=True, indoor_f=70.7)
        assert z.heat.available
        assert not z.cool.available and z.cool.reason == zc.REASON_OUTDOOR_LOCKOUT and z.cool.policy_locked

    def test_cool_lockout_safety_release_boundary(self):
        at = _zone(cool_lockout_engaged=True, indoor_f=71.6 + 5.0)  # depth == tolerance: stays locked (R1)
        past = _zone(cool_lockout_engaged=True, indoor_f=71.6 + 5.01)
        assert not at.cool.available
        assert past.cool.available and past.cool.safety_released and past.cool.reason == zc.REASON_OUTDOOR_LOCKOUT
        assert not past.bank_allowed(zc.FAMILY_COOL)  # a bank stays off during a safety release

    def test_heat_lockout_safety_release_boundary(self):
        at = _zone(heat_lockout_engaged=True, indoor_f=64.4 - 5.0)
        past = _zone(heat_lockout_engaged=True, indoor_f=64.4 - 5.01)
        assert not at.heat.available and at.cool.available
        assert past.heat.available and past.heat.safety_released

    def test_unknown_indoor_or_band_keeps_the_lock(self):
        assert not _zone(cool_lockout_engaged=True, indoor_f=None).cool.available
        assert not _zone(cool_lockout_engaged=True, indoor_f=90.0, ceiling_f=None).cool.available
        assert not _zone(cool_lockout_engaged=True, indoor_f=90.0, tolerance_f=None).cool.available

    def test_season_heat_only_and_cool_only(self):
        heat_only = _zone(season=zc.SEASON_HEAT_ONLY, indoor_f=70.0)
        cool_only = _zone(season=zc.SEASON_COOL_ONLY, indoor_f=70.0)
        assert heat_only.heat.available and not heat_only.cool.available
        assert heat_only.cool.reason == zc.REASON_SEASON
        assert cool_only.cool.available and not cool_only.heat.available
        assert cool_only.heat.reason == zc.REASON_SEASON

    def test_season_reason_wins_over_outdoor(self):
        z = _zone(season=zc.SEASON_HEAT_ONLY, cool_lockout_engaged=True, indoor_f=70.0)
        assert z.cool.reason == zc.REASON_SEASON

    def test_heat_only_equipment_with_season_cool_only_has_nothing(self):
        z = _zone(equipment_cool=False, season=zc.SEASON_COOL_ONLY, indoor_f=70.0)
        assert not z.heat.available and not z.cool.available
        assert z.cool.reason == zc.REASON_EQUIPMENT and z.heat.reason == zc.REASON_SEASON

    def test_unknown_season_value_is_auto(self):
        assert zc.normalize_season("summer") == zc.SEASON_AUTO
        assert zc.normalize_season(None) == zc.SEASON_AUTO

    def test_band_modes_need_both_sides_and_masking(self):
        z = _zone(cool_lockout_engaged=True, indoor_f=70.0)
        assert not z.mode_allowed("heat_cool") and not z.mode_allowed("auto")
        assert z.mode_allowed("off") and z.mode_allowed("fan_only")
        assert zc.masked_modes(("off", "heat", "cool", "heat_cool"), z) == ("off", "heat")

    def test_as_dict_shape(self):
        d = _zone(cool_lockout_engaged=True, indoor_f=70.0).cool.as_dict()
        assert d["available"] is False and d["reason"] == "outdoor_lockout" and d["policy_locked"] is True


# ---------------------------------------------------------------------------------------------------------------
# Selector leaf and FSM
# ---------------------------------------------------------------------------------------------------------------


def _leaf(**kw):
    base = {
        "current_family": "cooling",
        "default_family": "cooling",
        "indoor": 70.7,
        "floor": 64.4,
        "ceiling": 71.6,
        "margin_f": 2.0,
        "natural_vent_active": False,
        "whf_owns_hvac": False,
        "now": NOW,
    }
    base.update(kw)
    return ComfortFamilyInputs(**base)


def test_leaf_single_family_needs_no_indoor_reading():
    """A heat-only (or cool-locked) zone with no indoor reading still arms heat, not the current cool (#1489)."""
    d = decide_comfort_family(_leaf(indoor=None, supports_cool=False))
    assert d.target_family == "heating"


def test_fsm_leaving_an_unavailable_family_ignores_the_dwell_lock():
    inputs = ComfortFamilyFsmInputs(
        default_family="heating",
        indoor=70.7,
        floor=64.4,
        ceiling=71.6,
        margin_f=2.0,
        natural_vent_active=False,
        whf_owns_hvac=False,
        now=NOW,
        min_dwell_seconds=600.0,
        supports_cool=False,
        last_mode_change_at=NOW - timedelta(seconds=30),  # inside the dwell lock
    )
    result = transition(
        ComfortFamilyState.COOLING,
        ComfortFamilyEvent(kind=ComfortFamilyEventKind.TICK, inputs=inputs),
        dwell_state=ComfortFamilyDwellState(dwell_since=NOW - timedelta(seconds=30)),
    )
    assert not result.locked_out
    assert result.to_state is ComfortFamilyState.HEATING


# ---------------------------------------------------------------------------------------------------------------
# Real engine, headless
# ---------------------------------------------------------------------------------------------------------------

_CFG = {
    "comfort_heat": C(20.0),
    "comfort_cool": C(24.0),
    "setback_heat": C(17.0),
    "setback_cool": C(26.0),
    "sleep_heat": C(18.0),
    "sleep_cool": C(22.0),
    "fan_mode": "disabled",
}


def _engine(config: dict, *, state: str = "heat", indoor: float = 70.7, modes=("off", "heat", "cool", "heat_cool")):
    engine, hass, scheduler, events = build_headless_engine(
        config={**_CFG, **config},
        climate_entity=ENTITY,
        climate_state=state,
        climate_attributes={"current_temperature": indoor, "hvac_modes": list(modes), "temperature": 64.4},
        start_time=NOW,
    )
    return engine, hass, scheduler, events


def _climate_calls(hass, service: str | None = None) -> list[dict]:
    return [
        a for a in hass.action_log if a.get("domain") == "climate" and (service is None or a.get("service") == service)
    ]


class TestBackstop:
    def test_refused_write_is_capability_blocked_not_failed(self):
        engine, hass, scheduler, events = _engine({"cool_lockout_outdoor_temp": 60.8})
        with scheduler.installed():
            assert engine.update_outdoor_temp(44.6) is True
            mode = asyncio.run(engine._set_hvac_mode("cool", reason="test"))
            temp = asyncio.run(engine._set_temperature(71.6, reason="test", mode="cool"))
            scheduler.advance_by(900)  # past every verify / re-send / retry delay
        assert mode is HvacCommandResult.CAPABILITY_BLOCKED
        assert temp is HvacCommandResult.CAPABILITY_BLOCKED
        assert hvac_outcome_label(temp) == "not_sent"
        assert _climate_calls(hass) == []  # nothing reached the thermostat
        names = [e[0] for e in events]
        for failure in ("hvac_mode_not_applied", "hvac_command_failed", "setpoint_rejected", "engine_incident"):
            assert failure not in names
        assert engine._pending_restore is None
        assert engine._write_seq == 0  # never stamped as a write
        assert engine._mode_write_record(temp, "cool", 71.6).held_reason == "no_capable_mode"

    def test_heat_still_written_while_cool_is_locked(self):
        engine, hass, scheduler, _ = _engine({"cool_lockout_outdoor_temp": 60.8})
        with scheduler.installed():
            engine.update_outdoor_temp(44.6)
            result = asyncio.run(engine._set_temperature(64.4, reason="test", mode="heat"))
        assert result is not HvacCommandResult.CAPABILITY_BLOCKED
        assert [a["data"].get("hvac_mode") for a in _climate_calls(hass, "set_temperature")] == ["heat"]

    def test_band_mode_restore_is_refused_while_a_side_is_locked(self):
        engine, hass, scheduler, _ = _engine({"cool_lockout_outdoor_temp": 60.8})
        with scheduler.installed():
            engine.update_outdoor_temp(44.6)
            assert asyncio.run(engine._set_hvac_mode("heat_cool", reason="t")) is HvacCommandResult.CAPABILITY_BLOCKED

    def test_nothing_changes_without_settings(self):
        engine, hass, scheduler, _ = _engine({})
        with scheduler.installed():
            assert engine.update_outdoor_temp(20.0) is False
            assert asyncio.run(engine._set_hvac_mode("cool", reason="t")) is not HvacCommandResult.CAPABILITY_BLOCKED

    def test_mode_switch_before_a_setpoint_write_is_refused(self):
        """The nudge / retry path reaches the thermostat through ``_ensure_hvac_mode_for_write`` directly."""
        engine, hass, scheduler, _ = _engine({"cool_lockout_outdoor_temp": 60.8}, state="heat")
        with scheduler.installed():
            engine.update_outdoor_temp(44.6)
            assert asyncio.run(engine._ensure_hvac_mode_for_write("cool", engine._write_seq)) is False
        assert engine._last_mode_switch_result is HvacCommandResult.CAPABILITY_BLOCKED
        assert _climate_calls(hass) == []

    @pytest.mark.parametrize(
        ("config", "modes", "live", "outdoor"),
        [
            ({"hvac_season": "cool_only"}, ("off", "heat"), "heat", None),  # heat-only equipment + season Cool only
            ({"cool_lockout_outdoor_temp": 60.8}, ("off", "cool"), "cool", 44.6),  # cool-only equipment + cool lockout
            ({"hvac_season": "cool_only", "cool_lockout_outdoor_temp": 60.8}, ("off", "heat", "cool"), "cool", 44.6),
        ],
    )
    def test_zone_with_neither_family_turns_the_thermostat_off(self, config, modes, live, outdoor):
        """Neither side available: the thermostat is not left running the locked mode -- it is switched Off."""
        engine, hass, scheduler, _ = _engine(config, state=live, modes=modes)
        from custom_components.climate_advisor.automation import ComfortBand

        with scheduler.installed():
            if outdoor is not None:
                engine.update_outdoor_temp(outdoor)
            asyncio.run(engine._apply_comfort_band(ComfortBand(64.4, 71.6, "floor", "t"), reason="t"))
        assert engine._last_band_write.held_reason == "no_capable_mode"
        assert engine._last_band_write.family == "off"
        assert [(a["service"], a["data"].get("hvac_mode")) for a in _climate_calls(hass)] == [("set_hvac_mode", "off")]

    def test_zone_with_neither_family_already_off_writes_nothing(self):
        engine, hass, scheduler, _ = _engine({"hvac_season": "cool_only"}, state="off", modes=("off", "heat"))
        from custom_components.climate_advisor.automation import ComfortBand

        with scheduler.installed():
            asyncio.run(engine._apply_comfort_band(ComfortBand(64.4, 71.6, "floor", "t"), reason="t"))
        assert engine._last_band_write.result is None
        assert _climate_calls(hass) == []

    def test_ode_ceiling_guard_does_not_escalate_while_cool_is_locked(self):
        from types import SimpleNamespace

        from custom_components.climate_advisor.ode_ceiling_guard import OdeCeilingGuardOutcome

        engine, hass, scheduler, events = _engine({"cool_lockout_outdoor_temp": 60.8}, state="heat", indoor=71.0)
        engine._last_comfort_band = None  # no band yet: the selector-preview skip cannot help
        decision = SimpleNamespace(
            ceiling_decision=SimpleNamespace(
                outcome=OdeCeilingGuardOutcome.ESCALATE,
                ceiling=71.6,
                outdoor=44.6,
                indoor=71.0,
                breach_ts=NOW + timedelta(hours=1),
                hours_to_breach=1.0,
                lead_min=30.0,
            )
        )
        with scheduler.installed():
            engine.update_outdoor_temp(44.6)
            asyncio.run(engine._apply_ode_ceiling_guard_decision(_classification("mild", "off"), [], decision))
        assert "ceiling_guard_fired" not in [e[0] for e in events]
        assert _climate_calls(hass) == []

    def test_blind_restore_of_a_locked_mode_goes_through_the_selector(self):
        engine, hass, scheduler, _ = _engine({"cool_lockout_outdoor_temp": 60.8}, state="off")
        engine._current_classification = _classification("cold", "heat")
        with scheduler.installed():
            engine.update_outdoor_temp(44.6)
            asyncio.run(engine._restore_hvac_mode_gated("cool", reason="t"))
        modes = [a["data"].get("hvac_mode") for a in _climate_calls(hass)]
        assert "cool" not in modes and "heat" in modes


def _classification(day_type: str, hvac_mode: str) -> DayClassification:
    return DayClassification(
        day_type=day_type,
        trend_direction="stable",
        trend_magnitude=0.0,
        today_high=50.0,
        today_low=40.0,
        tomorrow_high=50.0,
        tomorrow_low=40.0,
        hvac_mode=hvac_mode,
    )


class TestBanksSuppressed:
    def test_tou_home_cool_bank_suppressed_and_selector_not_deferred(self):
        engine, hass, scheduler, events = _engine({"cool_lockout_outdoor_temp": 60.8}, state="cool")
        with scheduler.installed():
            engine.update_outdoor_temp(44.6)
            engine.set_tou_precondition_window(NOW + timedelta(hours=2), "cool")
            assert engine._tou_precondition_active(NOW) is False  # the bank owns nothing
            asyncio.run(engine.apply_tou_precondition(_classification("hot", "cool"), 68.0, "s1"))
        assert _climate_calls(hass) == []
        assert "tou_precondition_applied" not in [e[0] for e in events]  # suppressed, not attempted

    def test_tou_away_cool_bank_suppressed(self):
        engine, hass, scheduler, events = _engine({"cool_lockout_outdoor_temp": 60.8}, state="cool")
        engine._occupancy_mode = "away"
        with scheduler.installed():
            engine.update_outdoor_temp(44.6)
            asyncio.run(engine.apply_tou_away_vacation_precondition(_classification("hot", "cool"), 70.0, "s1", "cool"))
        assert _climate_calls(hass) == []
        assert "tou_av_precondition_applied" not in [e[0] for e in events]

    def test_tou_bank_runs_without_a_lockout(self):
        """Control: the same bank is written with no lockout (the suppression above is the lockout's doing)."""
        engine, hass, scheduler, _ = _engine({}, state="cool")
        with scheduler.installed():
            engine.update_outdoor_temp(44.6)
            asyncio.run(engine.apply_tou_precondition(_classification("hot", "cool"), 68.0, "s1"))
        assert _climate_calls(hass, "set_temperature")

    def test_pre_cool_suppressed(self):
        engine, hass, scheduler, _ = _engine(
            {"cool_lockout_outdoor_temp": 60.8, "sleep_time": "22:30", "wake_time": "06:30"}, state="cool"
        )
        engine._current_classification = _classification("hot", "cool")
        with scheduler.installed():
            engine.update_outdoor_temp(44.6)
            result = asyncio.run(engine.handle_pre_cool(70.0, False, modifier=-2.0))
        assert result.code == "skipped_cool_unavailable"
        assert _climate_calls(hass) == []

    def test_season_heat_only_suppresses_pre_cool(self):
        engine, hass, scheduler, _ = _engine({"hvac_season": "heat_only"}, state="cool")
        engine._current_classification = _classification("hot", "cool")
        with scheduler.installed():
            result = asyncio.run(engine.handle_pre_cool(70.0, False, modifier=-2.0))
        assert result.code == "skipped_cool_unavailable"


class TestEventsStatusDiagnostics:
    def test_engage_and_release_rows_once_per_change(self):
        engine, hass, scheduler, events = _engine({"cool_lockout_outdoor_temp": 60.8})
        with scheduler.installed():
            for outdoor in (44.6, 44.0, 50.0, 63.0, 64.4, 66.0):  # 63 < 64.4 release point
                engine.update_outdoor_temp(outdoor)
        rows = [(e[0], e[1]["family"], e[1]["reason"]) for e in events if e[0].startswith("hvac_lockout")]
        assert rows == [
            ("hvac_lockout_engaged", "cool", "outdoor_lockout"),
            ("hvac_lockout_released", "cool", "outdoor_lockout"),
        ]
        engaged = next(e[1] for e in events if e[0] == "hvac_lockout_engaged")
        assert engaged["outdoor_f"] == 44.6 and engaged["threshold_f"] == 60.8

    def test_status_line_in_the_users_unit(self):
        engine, hass, scheduler, _ = _engine({"cool_lockout_outdoor_temp": C(16.0), "temp_unit": "celsius"})
        with scheduler.installed():
            engine.update_outdoor_temp(C(3.0))
        line = engine.zone_lockout_status()
        assert line is not None and line.code == "cool_outdoor"
        assert str(line) == "Cooling off: outdoor 3°C (below 16°C)"

    def test_status_line_for_the_season(self):
        engine, *_ = _engine({"hvac_season": "cool_only"})
        line = engine.zone_lockout_status()
        assert line is not None and line.code == "heat_season"

    def test_no_status_line_without_a_lock(self):
        engine, *_ = _engine({})
        assert engine.zone_lockout_status() is None

    def test_snapshot_shows_effective_and_equipment_stays_unmasked(self):
        engine, hass, scheduler, _ = _engine({"cool_lockout_outdoor_temp": 60.8})
        with scheduler.installed():
            engine.update_outdoor_temp(44.6)
        snap = engine.zone_capability_snapshot()
        assert snap["cool"]["available"] is False and snap["cool"]["reason"] == "outdoor_lockout"
        assert snap["heat"]["available"] is True and snap["cool_lockout_engaged"] is True
        assert engine._get_thermostat_capabilities().supports_cool is True  # equipment input unchanged
        assert engine._effective_capabilities().supports_cool is False

    def test_activity_rows_render(self):
        from custom_components.climate_advisor.ai_skills_context import EVENT_RENDERERS

        label, settings = EVENT_RENDERERS["hvac_lockout_engaged"](
            {"family": "cool", "reason": "outdoor_lockout", "outdoor_f": 44.6, "threshold_f": 60.8}, "fahrenheit"
        )
        assert "outdoor lockout" in label and "44.6" in settings and "60.8" in settings
        label, settings = EVENT_RENDERERS["hvac_lockout_released"](
            {"family": "cool", "reason": "outdoor_lockout", "safety": True, "depth_f": 5.5}, "fahrenheit"
        )
        assert "past the tolerance" in label and "5.5" in settings


# ---------------------------------------------------------------------------------------------------------------
# Production harness replays (#1560), real coordinator + engine
# ---------------------------------------------------------------------------------------------------------------


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _night(outdoor_at, *, indoor_points, config=None, start=datetime(2026, 11, 11, 21, 0), end=None, day_type="cold"):
    """A cold night on an 18-22 °C sleep band: temp_update every 10 min, plan cycle each half hour."""
    end = end or indoor_points[-1][0]
    events = [
        {
            "time": _ts(start - timedelta(minutes=1)),
            "type": "temp_update",
            "indoor_f": indoor_points[0][1],
            "outdoor_f": outdoor_at(start),
        },
        {
            "time": _ts(start - timedelta(seconds=30)),
            "type": "classification",
            "day_type": day_type,
            "hvac_mode": "heat",
            "windows_recommended": False,
        },
    ]
    t = start
    while t <= end:
        for (t0, v0), (t1, v1) in zip(indoor_points, indoor_points[1:], strict=False):
            if t0 <= t <= t1:
                v = v0 + (v1 - v0) * ((t - t0).total_seconds() / max((t1 - t0).total_seconds(), 1))
                break
        ev = {"time": _ts(t), "type": "temp_update", "indoor_f": round(v, 2), "outdoor_f": outdoor_at(t)}
        if t.minute % 30 == 0:
            ev["plan_cycle"] = True
        events.append(ev)
        t += timedelta(minutes=10)
    cfg = {
        **_CFG,
        "sleep_time": "22:30",
        "wake_time": "06:30",
        "thermostat_hvac_modes": ["off", "heat", "cool", "heat_cool"],
        "thermostat_supported_features": 1,
        "initial_thermostat_mode": "heat",
    }
    cfg.update(config or {})
    return {
        "name": "nov12_1699",
        "use_coordinator": True,
        "skip_startup_coalesce": True,
        "config": cfg,
        "events": events,
        "assertions": [],
    }


NOV12_INDOOR = [
    (datetime(2026, 11, 11, 21, 0), C(21.6)),
    (datetime(2026, 11, 12, 0, 20), C(21.5)),  # 70.7 F, inside the 2 F cool margin of the 71.6 F sleep ceiling
    (datetime(2026, 11, 12, 6, 0), C(19.6)),
]


def _mode_writes(result, mode: str) -> list:
    return [a for a in result.action_log if a.get("domain") == "climate" and a.get("data", {}).get("hvac_mode") == mode]


@pytest.fixture(scope="module")
def nov12_control():
    return run_production_scenario(_night(lambda t: C(7.0), indoor_points=NOV12_INDOOR), use_coordinator=True)


@pytest.fixture(scope="module")
def nov12_locked():
    sc = _night(lambda t: C(7.0), indoor_points=NOV12_INDOOR, config={"cool_lockout_outdoor_temp": C(16.0)})
    return run_production_scenario(sc, use_coordinator=True)


class TestNov12Replay:
    def test_control_arms_cool_on_the_cold_night(self, nov12_control):
        """Main today (no lockout): the margin arming switches the thermostat to cool. Proves the replay bites."""
        assert _mode_writes(nov12_control, "cool")

    def test_cool_lockout_never_writes_cool_and_keeps_heat_armed(self, nov12_locked):
        assert nov12_locked.callback_errors == []
        assert _mode_writes(nov12_locked, "cool") == []
        heat = _mode_writes(nov12_locked, "heat")
        assert heat and all(a["data"].get("temperature") is None or a["data"]["temperature"] <= 68.0 for a in heat)
        # Heat stays ARMED all night: every half-hour cycle from midnight to 06:00 writes the 64.4 F sleep floor.
        night = {
            a["ts"]
            for a in heat
            if datetime(2026, 11, 12, 0, 0, tzinfo=UTC) <= a["ts"] <= datetime(2026, 11, 12, 6, 0, tzinfo=UTC)
            and a["data"].get("temperature") == 64.4
        }
        assert len(night) >= 12
        engaged = [p for et, p, _ in nov12_locked.event_log if et == "hvac_lockout_engaged"]
        assert len(engaged) == 1 and engaged[0]["family"] == "cool"


def test_lock_engaging_while_cool_is_armed_switches_to_heat_at_once():
    """Outdoor drops through the 16 °C lockout at 23:05 with cool armed: heat is written right away, not at 23:30."""
    drop = datetime(2026, 11, 11, 23, 5, tzinfo=UTC)

    def outdoor(t):
        return C(20.0) if t.replace(tzinfo=UTC) < drop else C(7.0)

    sc = _night(
        outdoor,
        indoor_points=NOV12_INDOOR,
        config={"cool_lockout_outdoor_temp": C(16.0)},
        end=datetime(2026, 11, 12, 1, 0),
    )
    # A reading at the drop minute itself, with no plan cycle: only the availability change can act on it.
    sc["events"].append({"time": _ts(drop), "type": "temp_update", "outdoor_f": C(7.0)})  # no indoor change
    r = run_production_scenario(sc, use_coordinator=True)
    assert r.callback_errors == []
    cool_before = [a for a in _mode_writes(r, "cool") if a["ts"] < drop]
    assert cool_before, "the scenario must have cool armed before the lockout engages"
    assert [a for a in _mode_writes(r, "cool") if a["ts"] >= drop] == []
    first_heat = min(a["ts"] for a in _mode_writes(r, "heat") if a["ts"] >= drop)
    assert first_heat - drop < timedelta(minutes=5)  # the next plan cycle would be 23:30


def test_heat_lockout_warm_day_cold_house_heats_past_the_tolerance_then_locks_again():
    """Heat lockout 60 F, outdoor 70 F (locked). The house falls more than the 2 F mild-day tolerance below the 68 F
    floor: heat runs (safety release). It keeps running while the house is still below the floor (the release's
    hysteresis); once the house is back at the floor the heat lock returns and cool is armed again."""
    start = datetime(2026, 11, 12, 9, 0)
    points = [
        (start, 70.0),
        (start + timedelta(hours=1), 65.0),  # 3 F below the floor: past the 2 F tolerance
        (start + timedelta(hours=2), 65.0),
        (start + timedelta(hours=2, minutes=30), 67.0),  # back inside the tolerance, still below the floor
        (start + timedelta(hours=3), 69.0),  # back at / above the floor
        (start + timedelta(hours=4), 69.0),
    ]
    sc = _night(
        lambda t: 70.0,
        indoor_points=points,
        start=start,
        day_type="mild",
        config={"heat_lockout_outdoor_temp": 60.0, "sleep_time": "23:00", "wake_time": "06:00"},
    )
    r = run_production_scenario(sc, use_coordinator=True)
    assert r.callback_errors == []
    released_at = start.replace(tzinfo=UTC) + timedelta(minutes=50)  # first reading below 66 F
    heat = _mode_writes(r, "heat")
    assert heat, "a house past the tolerance must get heat despite the lockout"
    assert min(a["ts"] for a in heat) >= released_at - timedelta(minutes=10)
    still_released = start.replace(tzinfo=UTC) + timedelta(hours=2, minutes=30)
    assert [a for a in heat if a["ts"] >= still_released], "inside the tolerance but below the floor: heat continues"
    relocked = start.replace(tzinfo=UTC) + timedelta(hours=3)
    assert [a for a in heat if a["ts"] > relocked + timedelta(minutes=10)] == []
    kinds = [(et, p.get("safety")) for et, p, _ in r.event_log if et.startswith("hvac_lockout")]
    assert ("hvac_lockout_released", True) in kinds and ("hvac_lockout_engaged", True) in kinds


def test_occupant_cool_change_is_not_reversed_by_the_lockout():
    """The occupant switches the thermostat to cool by hand while cooling is locked out: Climate Advisor does not
    switch it back (manual override precedence is unchanged; the lockout only limits CA's own commands)."""
    sc = _night(
        lambda t: C(7.0),
        indoor_points=NOV12_INDOOR,
        config={"cool_lockout_outdoor_temp": C(16.0)},
        end=datetime(2026, 11, 12, 0, 30),
    )
    sc["events"].append(
        {"time": "2026-11-11T23:12:00", "type": "thermostat_state_changed", "hvac_mode": "cool", "temperature": C(21.0)}
    )
    r = run_production_scenario(sc, use_coordinator=True)
    t_user = datetime(2026, 11, 11, 23, 12, tzinfo=UTC)
    after = [
        a
        for a in r.action_log
        if a.get("domain") == "climate"
        and a["ts"] > t_user
        and a.get("service") in ("set_hvac_mode", "set_temperature")
    ]
    grace_end = next(ts for et, _p, ts in r.event_log if et == "grace_expired")
    assert grace_end > t_user
    # Nothing is written over the occupant's cool for the whole manual grace; only after it ends does the plan resume.
    assert [a for a in after if a["ts"] < grace_end] == []
    assert any(et == "override_confirmed" for et, _p, _t in r.event_log)


# ---------------------------------------------------------------------------------------------------------------
# Options flow (real step handler)
# ---------------------------------------------------------------------------------------------------------------

_SETPOINTS_C = {
    "comfort_heat": 20.0,
    "comfort_cool": 24.0,
    "setback_heat": 17.0,
    "setback_cool": 26.0,
    "sleep_heat": 18.0,
    "sleep_cool": 22.0,
}


def _submit_setpoints(entry: dict, user_input: dict):
    from tests.test_config_flow import _make_options_flow

    flow, captured = _make_options_flow(entry)
    result = asyncio.run(flow.async_step_setpoints(user_input))
    return result, captured


def _celsius_entry() -> dict:
    from tests.test_config_flow import FULL_CONFIG

    return {**FULL_CONFIG, "temp_unit": "celsius", **{k: C(v) for k, v in _SETPOINTS_C.items()}}


def test_options_reject_a_heat_lockout_at_or_below_the_cool_lockout():
    result, captured = _submit_setpoints(
        _celsius_entry(),
        {**_SETPOINTS_C, "cool_lockout_outdoor_temp": 16.0, "heat_lockout_outdoor_temp": 16.0, "hvac_season": "auto"},
    )
    assert result["errors"] == {"heat_lockout_outdoor_temp": "lockout_overlap"}
    assert "data" not in captured or "cool_lockout_outdoor_temp" not in captured.get("data", {})


def test_options_store_lockouts_in_fahrenheit_and_season():
    result, captured = _submit_setpoints(
        _celsius_entry(),
        {
            **_SETPOINTS_C,
            "cool_lockout_outdoor_temp": 16.0,
            "heat_lockout_outdoor_temp": 18.0,
            "hvac_season": "heat_only",
        },
    )
    data = captured["data"]
    assert data["cool_lockout_outdoor_temp"] == pytest.approx(60.8)
    assert data["heat_lockout_outdoor_temp"] == pytest.approx(64.4)
    assert data["hvac_season"] == "heat_only"


def test_options_blank_lockout_clears_it():
    entry = {**_celsius_entry(), "cool_lockout_outdoor_temp": 60.8}
    _result, captured = _submit_setpoints(entry, {**_SETPOINTS_C, "hvac_season": "auto"})
    assert "cool_lockout_outdoor_temp" not in captured["data"]


def test_options_form_offers_the_fields_off_by_default():
    from tests.test_config_flow import _make_options_flow

    flow, _ = _make_options_flow(_celsius_entry())
    form = asyncio.run(flow.async_step_setpoints(None))
    keys = {str(k): k for k in form["data_schema"].schema}
    assert keys["hvac_season"].default() == "auto"
    assert "cool_lockout_outdoor_temp" in keys and "heat_lockout_outdoor_temp" in keys
    assert (keys["cool_lockout_outdoor_temp"].description or {}).get("suggested_value") is None


def test_spec_variable_table_matches_the_code():
    """R9 for this territory: the spec's variable table names the real defaults."""
    from pathlib import Path

    from custom_components.climate_advisor import const

    spec = (Path(__file__).resolve().parent.parent / "docs" / "zone-capability-spec.md").read_text(encoding="utf-8")
    assert f"| `LOCKOUT_RELEASE_HYSTERESIS_F` | {zc.LOCKOUT_RELEASE_HYSTERESIS_F} |" in spec
    assert f"| `hvac_season` | `{const.DEFAULT_HVAC_SEASON}` |" in spec
    assert const.HVAC_SEASON_OPTIONS == zc.SEASONS
    for key in (const.CONF_COOL_LOCKOUT_OUTDOOR_TEMP, const.CONF_HEAT_LOCKOUT_OUTDOOR_TEMP, const.CONF_HVAC_SEASON):
        assert f"`{key}`" in spec and key in const.CONFIG_METADATA


# ---------------------------------------------------------------------------------------------------------------
# Review fixes (#1699 phase 1 review)
# ---------------------------------------------------------------------------------------------------------------


class TestSafetyReleaseHysteresis:
    def test_released_stays_released_until_back_at_the_edge(self):
        assert zc.next_safety_release(False, 5.0, 5.0) is False  # == tolerance: locked
        assert zc.next_safety_release(False, 5.01, 5.0) is True
        assert zc.next_safety_release(True, 4.9, 5.0) is True  # back inside the tolerance: still released
        assert zc.next_safety_release(True, 0.01, 5.0) is True
        assert zc.next_safety_release(True, 0.0, 5.0) is False  # at the edge: locks again

    def test_relock_waits_for_the_dwell(self):
        assert zc.next_safety_release(True, -1.0, 5.0, relock_allowed=False) is True
        assert zc.next_safety_release(True, -1.0, 5.0, relock_allowed=True) is False

    def test_unknown_reading_locks(self):
        assert zc.next_safety_release(True, None, 5.0) is False

    def test_hovering_around_ceiling_plus_tolerance_does_not_alternate(self):
        """Cold night, cool lockout, house hovering at the sleep ceiling + 5 F (the cold-day tolerance): the safety
        release switches to cool ONCE and stays; before the fix every 0.1 F tick flipped cool and heat."""
        start = datetime(2026, 11, 12, 0, 0)
        hover = [(start + timedelta(minutes=10 * i), 71.6 + (5.1 if i % 2 else 4.9)) for i in range(1, 25)]
        sc = _night(
            lambda t: C(7.0),
            indoor_points=[(start, 71.6 + 5.2), *hover],
            start=start,
            config={"cool_lockout_outdoor_temp": C(16.0)},
        )
        r = run_production_scenario(sc, use_coordinator=True)
        assert r.callback_errors == []
        switches = [(p["from_mode"], p["to_mode"]) for et, p, _ in r.event_log if et == "thermostat_mode_set"]
        assert switches.count(("cool", "heat")) == 0, switches
        assert switches.count(("heat", "cool")) <= 1, switches
        assert _mode_writes(r, "cool"), "the house past the tolerance still gets cooling"


def test_engine_relock_waits_for_the_mode_switch_dwell():
    """Released cool, house back at the ceiling: within the dwell since the last mode change it stays released (no
    instant flip back to heat); after the dwell it locks."""
    engine, hass, scheduler, _ = _engine({"cool_lockout_outdoor_temp": 60.8}, state="heat", indoor=80.0)

    def decide(indoor):
        engine._comfort_family_inputs("cool", floor=64.4, ceiling=71.6, now=NOW, day_type="cold", indoor=indoor)
        return engine._zone_safety_released["cool"]

    with scheduler.installed():
        engine.update_outdoor_temp(44.6)
        assert decide(80.0) is True
        asyncio.run(engine._set_hvac_mode("cool", reason="t"))  # the release's own mode change starts the dwell
        assert decide(71.0) is True  # back at the edge, inside the dwell
        scheduler.advance_by(601)
        assert decide(71.0) is False


class TestRecordedDecision:
    def test_backstop_trusts_the_selectors_release(self):
        """The selector released cool on its band; a write with no resolvable band is not refused against another."""
        engine, hass, scheduler, events = _engine({"cool_lockout_outdoor_temp": 60.8}, state="heat", indoor=80.0)
        with scheduler.installed():
            engine.update_outdoor_temp(44.6)
            engine._comfort_family_inputs("cool", floor=64.4, ceiling=71.6, now=NOW, day_type="cold", indoor=80.0)
            assert engine._zone_safety_released["cool"] is True
            result = asyncio.run(engine._set_hvac_mode("cool", reason="t"))
        assert result is not HvacCommandResult.CAPABILITY_BLOCKED
        assert [e for e in events if e[0] == "hvac_lockout_released" and e[1].get("safety")]

    def test_diagnostics_never_move_the_release(self):
        engine, hass, scheduler, events = _engine({"cool_lockout_outdoor_temp": 60.8}, state="heat", indoor=90.0)
        engine._current_classification = _classification("cold", "heat")
        with scheduler.installed():
            engine.update_outdoor_temp(44.6)
            snap = engine.zone_capability_snapshot()
            asyncio.run(engine._set_hvac_mode("cool", reason="t"))
        assert snap["cool"]["safety_released"] is False
        assert engine._zone_safety_released["cool"] is False
        assert not [e for e in events if e[1].get("safety")]

    def test_policy_change_resets_the_release(self):
        engine, hass, scheduler, _ = _engine({"cool_lockout_outdoor_temp": 60.8}, indoor=80.0)
        with scheduler.installed():
            engine.update_outdoor_temp(44.6)
            engine._comfort_family_inputs("cool", floor=64.4, ceiling=71.6, now=NOW, day_type="cold", indoor=80.0)
            engine.update_outdoor_temp(70.0)  # lockout releases
            engine.update_outdoor_temp(44.6)  # and engages again: starts locked
        assert engine._zone_safety_released["cool"] is False


class TestPreCoolHold:
    def test_held_pre_cool_band_ends_when_cool_locks(self):
        from custom_components.climate_advisor.automation import BAND_CONTEXT_SLEEP, ComfortBand

        engine, hass, scheduler, _ = _engine({"cool_lockout_outdoor_temp": 60.8, "wake_time": "06:30"})
        sleep_band = ComfortBand(64.4, 71.6, "ceiling", "sleep", context=BAND_CONTEXT_SLEEP)
        engine._pre_cool_band_in_force = ComfortBand(64.4, 68.0, "ceiling", "pre-cool", context=BAND_CONTEXT_SLEEP)
        engine._pre_cool_band_since = NOW
        with scheduler.installed():
            assert engine._hold_pre_cool_band(sleep_band).ceiling == 68.0  # no lock yet: the bank holds
            engine.update_outdoor_temp(44.6)
            assert engine._hold_pre_cool_band(sleep_band) is sleep_band
        assert engine._pre_cool_band_in_force is None


class TestObserveOnlyAndGrace:
    def test_observe_only_answers_dry_run_first(self):
        engine, hass, scheduler, _ = _engine({"cool_lockout_outdoor_temp": 60.8})
        engine.dry_run = True
        with scheduler.installed():
            engine.update_outdoor_temp(44.6)
            assert asyncio.run(engine._set_hvac_mode("cool", reason="t")) is HvacCommandResult.DRY_RUN
            assert asyncio.run(engine._set_temperature(71.6, reason="t", mode="cool")) is HvacCommandResult.DRY_RUN

    def test_refused_restore_starts_no_automation_grace(self):
        engine, hass, scheduler, _ = _engine({"cool_lockout_outdoor_temp": 60.8})
        with scheduler.installed():
            engine._start_automation_grace_unless_restore_unsent("t", HvacCommandResult.CAPABILITY_BLOCKED, None)
        assert engine._grace_active is False


class TestBandModes:
    def test_heat_only_auto_schedule_mode_is_allowed_without_options(self):
        """Tado / Netatmo ``[off, heat, auto]``: ``auto`` is the schedule mode, never refused on equipment alone."""
        engine, hass, scheduler, _ = _engine({}, modes=("off", "heat", "auto"))
        with scheduler.installed():
            assert asyncio.run(engine._set_hvac_mode("auto", reason="t")) is not HvacCommandResult.CAPABILITY_BLOCKED
        assert engine._effective_capabilities() == engine._get_thermostat_capabilities()

    def test_auto_refused_while_a_policy_lock_is_engaged(self):
        engine, hass, scheduler, _ = _engine({"hvac_season": "cool_only"}, modes=("off", "heat", "auto"))
        with scheduler.installed():
            assert asyncio.run(engine._set_hvac_mode("auto", reason="t")) is HvacCommandResult.CAPABILITY_BLOCKED

    def test_pure_band_mode_rule(self):
        heat_only = _zone(equipment_cool=False, indoor_f=70.0)
        assert heat_only.mode_allowed("auto") is True
        # A season that removes nothing the equipment has (Heat only on a heat-only unit) engages no policy lock.
        assert _zone(equipment_cool=False, season=zc.SEASON_HEAT_ONLY, indoor_f=70.0).mode_allowed("auto") is True
        assert _zone(equipment_cool=False, season=zc.SEASON_COOL_ONLY, indoor_f=70.0).mode_allowed("auto") is False


class TestGatedRestoreRecord:
    def test_records_the_family_the_selector_wrote(self):
        engine, hass, scheduler, _ = _engine({"cool_lockout_outdoor_temp": 60.8}, state="off")
        engine._current_classification = _classification("cold", "heat")
        with scheduler.installed():
            engine.update_outdoor_temp(44.6)
            asyncio.run(engine._restore_hvac_mode_gated("cool", reason="t"))
        assert engine._last_gated_restore_mode == "heat"

    def test_records_nothing_without_a_classification(self):
        engine, hass, scheduler, _ = _engine({"cool_lockout_outdoor_temp": 60.8}, state="off")
        with scheduler.installed():
            engine.update_outdoor_temp(44.6)
            assert asyncio.run(engine._restore_hvac_mode_gated("cool", reason="t")) is None
        assert engine._last_gated_restore_mode is None


# ---------------------------------------------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------------------------------------------


def _coordinator(config: dict):
    from tools.sim_harness.build_coordinator import build_headless_coordinator

    return build_headless_coordinator(
        config={**_CFG, **config},
        climate_entity=ENTITY,
        climate_state="cool",
        climate_attributes={"current_temperature": 70.7, "hvac_modes": ["off", "heat", "cool"], "temperature": 71.6},
        start_time=NOW,
        skip_startup_coalesce=True,
    )


def test_coordinator_runs_the_reactive_check_and_a_refresh_on_a_change():
    from unittest.mock import AsyncMock, patch

    coord, hass, scheduler, _ = _coordinator({"cool_lockout_outdoor_temp": 60.8})
    engine = coord.automation_engine
    with (
        scheduler.installed(),
        patch.object(engine, "comfort_family_temperature_check", new=AsyncMock()) as check,
        patch.object(coord, "async_request_refresh", new=AsyncMock()) as refresh,
    ):
        asyncio.run(coord._async_zone_capability_changed())
    check.assert_awaited_once()
    assert check.await_args.args[0] == pytest.approx(70.7)
    refresh.assert_awaited_once()


def test_coordinator_spawns_the_rerun_only_when_availability_changes():
    from unittest.mock import MagicMock, patch

    coord, hass, scheduler, _ = _coordinator({"cool_lockout_outdoor_temp": 60.8})
    spawned = []
    with (
        scheduler.installed(),
        patch.object(coord, "_spawn", new=MagicMock(side_effect=lambda c: (spawned.append(c), c.close()))),
    ):
        coord._apply_outdoor_temp(70.0, record_history=False)  # above the threshold: nothing changes
        assert spawned == []
        coord._apply_outdoor_temp(44.6, record_history=False)  # engages
        coord._apply_outdoor_temp(44.0, record_history=False)  # stays engaged
    assert len(spawned) == 1


def test_coordinator_tou_bank_in_a_locked_family_owns_nothing():
    from types import SimpleNamespace

    from custom_components.climate_advisor.scheduler import TOUPhase

    coord, hass, scheduler, _ = _coordinator({"cool_lockout_outdoor_temp": 60.8})
    coord._tou_phase_resolution = SimpleNamespace(
        phase=TOUPhase.PRECONDITIONING,
        mode="cool",
        target=68.0,
        schedule_start=NOW + timedelta(hours=2),
        precondition_start=NOW - timedelta(minutes=10),
        schedule_id="s1",
        window_end=NOW + timedelta(hours=5),
    )
    with scheduler.installed():
        assert coord._tou_bank_owns_setpoint() is True
        assert coord._tou_bank_in_force(NOW) is not None
        coord.automation_engine.update_outdoor_temp(44.6)
        assert coord._tou_bank_owns_setpoint() is False  # the band pass writes the band edge
        assert coord._tou_bank_in_force(NOW) is None  # current_hold shows the schedule band


def test_tou_suppression_logged_once_per_window(caplog):
    import logging

    engine, hass, scheduler, _ = _engine({"cool_lockout_outdoor_temp": 60.8}, state="cool")
    with scheduler.installed(), caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.automation"):
        engine.update_outdoor_temp(44.6)
        for _ in range(3):  # the 5-minute bank check asks again and again
            asyncio.run(engine.apply_tou_precondition(_classification("hot", "cool"), 68.0, "s1"))
            scheduler.advance_by(300)
    infos = [r for r in caplog.records if r.levelno == logging.INFO and "TOU pre-conditioning suppressed" in r.message]
    assert len(infos) == 1


def test_stranded_restore_records_the_mode_actually_written():
    """The fan already stopped with a stranded cool snapshot; cooling is locked out, so the selector arms heat. The
    Activity row names heat (what the thermostat got), not the cool snapshot."""
    engine, hass, scheduler, events = _engine(
        {"cool_lockout_outdoor_temp": 60.8, "fan_mode": "whole_house_fan", "fan_entity": "fan.whf"}, state="off"
    )
    engine._current_classification = _classification("cold", "heat")
    engine._pre_fan_hvac_mode = "cool"
    engine._fan_active = False
    with scheduler.installed():
        engine.update_outdoor_temp(44.6)
        asyncio.run(engine._deactivate_fan(reason="t", restore_hvac=True, release_suppression=True))
    rows = [p for et, p, _ in events if et == "stranded_hvac_suppression_restored"]
    assert rows and rows[-1]["restore_mode"] == "heat"
    assert "cool" not in [a["data"].get("hvac_mode") for a in _climate_calls(hass)]


def test_options_reject_a_heat_lockout_inside_the_hysteresis():
    """Cool 16 °C, heat 17 °C: both could be engaged between 17 and 18 °C, so the form refuses it."""
    result, captured = _submit_setpoints(
        _celsius_entry(),
        {**_SETPOINTS_C, "cool_lockout_outdoor_temp": 16.0, "heat_lockout_outdoor_temp": 17.0, "hvac_season": "auto"},
    )
    assert result["errors"] == {"heat_lockout_outdoor_temp": "lockout_overlap"}
