"""R6 golden-card follow-ups (Issue #1230): X4 low-grade v4, the no-fit note, the 5-minute time-of-use check.

Occupant view: a v4 whose fit is graded "low" (too few accepted thermostat starts in a used mode) still runs the
house, exactly as a v3 graded "low" does (the guard and the nat-vent plan read it; the "medium"-only gates stay
closed); a v4 still learning says plainly that the time-of-use bank and the window close time are on their defaults
too; and the time-of-use bank opens when its window starts, not up to half an hour later.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor import prediction_engines as pe
from custom_components.climate_advisor.scheduler import TOUPhase, TOUPhaseResolution
from tests.test_warm_day_comfort_gap import (
    _make_engine,
    _make_predicted_indoor,
    _make_warm_off_classification,
    _set_thermal_model,
)
from tools.sim_harness.scenario_model import v4_model_from_spec

_NOW = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
_V3_BASE = {"k_passive": -0.15, "k_active_cool": -2.0, "confidence": "high", "confidence_k_passive": "high"}


def _v4_src(confidence: str) -> SimpleNamespace:
    model = v4_model_from_spec({"confidence": confidence, "house": {"a_o": 0.15, "q_c": 2.0, "q_h": 2.0}})
    return SimpleNamespace(_v4_store=SimpleNamespace(v4_model=model), _v4_fit_running=False)


def _guard_decision(engine):
    curve = _make_predicted_indoor(start_hour_utc=10, temps=[76.0, 77.0, 78.0])
    with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
        mock_dt.now.return_value = _NOW
        return engine._resolve_classification_fsm_state(
            classification=_make_warm_off_classification(), predicted_indoor=curve
        )


def _engine():
    engine = _make_engine(config_overrides={"comfort_cool": 74.0}, indoor_temp=76.0, current_mode="off")
    engine._last_outdoor_temp = 88.0
    return engine


# ---------------------------------------------------------------------------------------------------- X4
def test_a_low_graded_v4_drives_the_ceiling_guard_exactly_like_a_low_graded_v3() -> None:
    """v4 "low" -> control grade "low": the guard decides on a breaching curve exactly as for v3 "low" (and acts)."""
    v3 = _engine()
    _set_thermal_model(v3, k_passive=-0.15, conf="low", k_active_cool=-2.0)

    v4 = _engine()
    v4._thermal_model = dict(_V3_BASE)  # v3's observations stay under v4's scalars
    src = _v4_src("low")
    v4._control_model_provider = lambda base: pe.control_model(src, {"primary_engine": "v4"}, base)
    assert v4._control_model()["confidence_k_passive"] == "low"  # v4's own grade reaches the decision

    d3, d4 = _guard_decision(v3).ceiling_decision, _guard_decision(v4).ceiling_decision
    assert d3 is not None and d4 is not None
    assert d4.outcome == d3.outcome  # the same decision class a "low" v3 gets
    assert d4.outcome.name == "ESCALATE", "a low-graded fit is eligible: the guard acts on the breaching curve"


def test_a_v4_graded_none_still_blanks_the_guard_and_a_low_one_keeps_the_medium_only_gates_closed() -> None:
    # nothing learned: the blank model (guard cannot act on rates it does not have)
    blank = pe.control_model(_v4_src("none"), {"primary_engine": "v4"}, dict(_V3_BASE))
    assert blank["k_passive"] is None and blank["confidence"] == "none"
    # low: usable, graded low, so the "medium"/"high" gates (nat-vent start veto's floor guard, TOU/pre-heat grade
    # checks) read it as low exactly as they read a low v3
    low = pe.control_model(_v4_src("low"), {"primary_engine": "v4"}, dict(_V3_BASE))
    assert low["confidence"] == "low" and low["k_passive"] == pytest.approx(-0.15)
    assert pe.usable(pe.ENGINES["v4"], _v4_src("low")) is True


def test_every_usable_grade_of_v4_maps_to_a_v3_grade_name() -> None:
    """No decision compares a raw v4 grade ("ok") with v3's names: control_model translates it first."""
    assert pe.control_model(_v4_src("ok"), {"primary_engine": "v4"}, dict(_V3_BASE))["confidence"] == "medium"
    assert pe.control_model(_v4_src("low"), {"primary_engine": "v4"}, dict(_V3_BASE))["confidence"] == "low"


# ---------------------------------------------------------------------------------------- card 10 note
def test_the_learning_note_names_the_bank_the_guard_and_the_window_close_time() -> None:
    cfg = {"primary_engine": "v4", "shadow_engine": "v3"}
    state = pe.engine_state(SimpleNamespace(_v4_store=SimpleNamespace(v4_model=None), _v4_fit_running=False), cfg)
    assert state["control"]["ready"] is False and state["control"]["source"] == "defaults"
    assert state["control"]["text"] == (
        "Climate Advisor is using its default plan for pre-heat, the time-of-use bank, the ceiling guard, "
        "setbacks and the window close time until v4 has learned your home."
    )


def test_no_fit_primary_does_not_read_v3s_hot_curve_in_the_guard() -> None:
    """Card 10, at the decision (the thermostat write cannot discriminate: the band arms cool before the guard).

    v3 has a good model and a curve that breaches; v4 is primary with no fit. The decisions read the blank model and
    so the guard has no rates to act on even when handed a breaching curve (the coordinator hands it the
    schedule estimate, not v3's curve, in production; the scenario card pins that end to end)."""
    engine = _engine()
    engine._thermal_model = dict(_V3_BASE)  # v3 is good
    src = SimpleNamespace(_v4_store=SimpleNamespace(v4_model=None), _v4_fit_running=False)
    engine._control_model_provider = lambda base: pe.control_model(src, {"primary_engine": "v4"}, base)
    hot = _make_predicted_indoor(start_hour_utc=10, temps=[76.0, 77.0, 78.0])  # v3 curve of the same cycle
    with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
        mock_dt.now.return_value = _NOW
        decision = engine._resolve_classification_fsm_state(
            classification=_make_warm_off_classification(), predicted_indoor=hot
        )
    assert decision.ceiling_decision is None or decision.ceiling_decision.outcome.name != "ESCALATE"
    # with v3 primary the very same model and a breaching curve does act: the dependency is the primary setting
    v3 = _engine()
    v3._thermal_model = dict(_V3_BASE)
    assert _guard_decision(v3).ceiling_decision.outcome.name == "ESCALATE"


# ----------------------------------------------------------------------------- X7: the 5-minute TOU check
def _coordinator(resolution: TOUPhaseResolution | None, now: datetime, *, thermostat=("cool", 68.0)):
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: PLC0415

    c = object.__new__(ClimateAdvisorCoordinator)
    # Issue #1395: the check reads the thermostat (mode, setpoint) to tell a held bank from a lost one.
    c.config = {"climate_entity": "climate.t", "temp_unit": "fahrenheit"}
    c.hass = MagicMock()
    state = MagicMock()
    state.state, state.attributes = thermostat[0], {"temperature": thermostat[1]}
    c.hass.states.get.return_value = state
    c._tou_phase_resolution = resolution
    c._current_classification = MagicMock()
    c._suppress_during_startup_coalescing = lambda _d: False
    c._resolve_tou_schedule_state = MagicMock()
    c._apply_tou_schedule = AsyncMock()
    return c


def _res(phase: TOUPhase, start: datetime, end: datetime) -> TOUPhaseResolution:
    return TOUPhaseResolution(phase, 68.0, "cool", "peak1", end, start)


def test_the_tou_bank_opens_on_the_first_check_after_its_window_starts() -> None:
    start, end = _NOW + timedelta(minutes=-1), _NOW + timedelta(minutes=105)
    c = _coordinator(_res(TOUPhase.NONE, start, end), _NOW)
    with patch("custom_components.climate_advisor.coordinator.dt_util") as dtu:
        dtu.now.return_value = _NOW
        asyncio.run(c._async_tou_bank_tick(_NOW))
    c._resolve_tou_schedule_state.assert_called_once()
    c._apply_tou_schedule.assert_awaited_once()


@pytest.mark.parametrize(
    ("phase", "start_off", "end_off"),
    [
        (TOUPhase.NONE, 20, 125),  # the window has not started
        (TOUPhase.PRECONDITIONING, -30, 75),  # already banking and the thermostat holds the bank target
        (TOUPhase.NONE, -200, -10),  # the peak already started
    ],
)
def test_the_tou_check_is_a_no_op_unless_a_window_just_opened(phase, start_off, end_off) -> None:
    c = _coordinator(_res(phase, _NOW + timedelta(minutes=start_off), _NOW + timedelta(minutes=end_off)), _NOW)
    with patch("custom_components.climate_advisor.coordinator.dt_util") as dtu:
        dtu.now.return_value = _NOW
        asyncio.run(c._async_tou_bank_tick(_NOW))
    c._apply_tou_schedule.assert_not_awaited()


@pytest.mark.parametrize(
    "thermostat",
    [("cool", 76.0), ("heat", 68.0), ("off", 68.0), ("cool", None)],
    ids=["comfort-edge-left-on", "wrong-mode", "off", "no-setpoint"],
)
def test_a_bank_the_thermostat_no_longer_holds_is_re_banked_from_its_state(thermostat) -> None:
    """Issue #1395: a briefing / fan-off re-assert / startup / Resume pass wrote the comfort edge mid-bank."""
    start, end = _NOW + timedelta(minutes=-30), _NOW + timedelta(minutes=75)
    c = _coordinator(_res(TOUPhase.PRECONDITIONING, start, end), _NOW, thermostat=thermostat)
    with patch("custom_components.climate_advisor.coordinator.dt_util") as dtu:
        dtu.now.return_value = _NOW
        asyncio.run(c._async_tou_bank_tick(_NOW))
    c._apply_tou_schedule.assert_awaited_once()


def test_the_tou_check_is_a_no_op_without_a_schedule_or_a_classification() -> None:
    c = _coordinator(None, _NOW)
    asyncio.run(c._async_tou_bank_tick(_NOW))
    c._apply_tou_schedule.assert_not_awaited()


# ------------------------------------------------------------------ X6: one story for the window plan
def test_a_window_open_steps_until_follows_its_periods_current_close() -> None:
    """The open step said "until about 4:46 PM" while the close step and the briefing said 11:00 AM.

    Occupant view: the plan's close moved earlier at a later cycle (the engine's cutoff arrived); the open step, kept
    as first promised and marked done, must name the close the list now shows."""
    from dataclasses import replace  # noqa: PLC0415

    from custom_components.climate_advisor import operation_timeline as ot  # noqa: PLC0415
    from tests.test_operation_timeline import _evening_cooling  # noqa: PLC0415

    base = ot.build_timeline(_evening_cooling())
    o = next(s for s in base.steps if s.kind == "windows_open")
    c = next(s for s in base.steps if s.kind == "windows_close")
    day = o.ts.replace(hour=0, minute=0, second=0, microsecond=0)
    open_step = replace(o, ts=day + timedelta(hours=7, minutes=1))
    late_close = replace(c, ts=day + timedelta(hours=16, minutes=46), meta={**c.meta, "id": "late"})
    early_close = replace(c, ts=day + timedelta(hours=11), meta={**c.meta, "id": "early"})
    # the latched open step was worded against the 16:46 close
    until = f" until about {ot._fmt_time(late_close.ts, late_close.ts)}"
    facts = {**open_step.facts, "end": ot._jsonable(late_close.ts), "until": until}
    open_step = replace(
        open_step,
        text=ot.say(
            open_step.reason, "fahrenheit", ref=late_close.ts, **{**facts, "t": open_step.ts, "end": late_close.ts}
        ),
        meta={**open_step.meta, "facts": facts},
    )
    assert open_step.text.endswith("until about 4:46 PM.")
    now = day + timedelta(hours=8)
    latched = replace(base, steps=(open_step, late_close))
    fresh = replace(base, steps=(early_close,))
    merged = ot._merge_latched(latched, fresh, now)
    texts = {s.kind: s for s in merged.steps if s.kind in ("windows_open", "windows_close")}
    assert texts["windows_close"].ts == early_close.ts
    assert texts["windows_open"].text.endswith("until about 11:00 AM.")
    assert texts["windows_open"].ts == open_step.ts and texts["windows_open"].status == "done"  # time/status untouched
