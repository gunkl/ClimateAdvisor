"""The ODE ceiling guard sends nothing when the thermostat already cools at or below its ceiling (Issue #1230 R6, X1).

Occupant view: on a warm "off" day the thermostat is already cooling at 74 F (the comfort band armed it). With the
primary engine's no-AC forecast (v4) the breach is predicted on every cycle, and the guard used to re-send "cool" plus
the same setpoint every two hours and, from bedtime, write 74 F under the 72 F sleep band each half hour (the bedroom
held at 74). The guard now acts only when the thermostat is off, in heat, or cooling ABOVE the ceiling; one rule for v3
and v4. These are the unit-level twins of ``r6b_warm_off_day_no_redundant_guard_writes__v3/__v4`` (golden drafts) and
demoted cards 1-5 (the guard can never be the writer of the 12:30 cool when the band already arms it).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.ode_ceiling_guard import OdeCeilingGuardOutcome
from tests.test_warm_day_comfort_gap import (
    _make_cg_engine,
    _make_predicted_indoor,
    _make_warm_off_classification,
    _set_thermal_model,
)

_NOW = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)
_BREACH = [73.0, 74.5, 76.0]  # breach of the 74 F ceiling ~1 h away, inside the 120 min fallback lead
_CALM = [72.0, 72.0, 72.0]


def _run(*, mode: str, setpoint: float | None, curve_temps: list[float] = _BREACH, cycles: int = 1, dry_run=False):
    engine: AutomationEngine = _make_cg_engine(comfort_cool=74.0, indoor_temp=73.0)
    engine.hass.states.get.return_value.state = mode
    if setpoint is not None:
        engine.hass.states.get.return_value.attributes["temperature"] = setpoint
    engine.dry_run = dry_run
    engine._last_outdoor_temp = 76.0
    _set_thermal_model(engine, k_passive=-0.05, conf="medium", k_active_cool=None)
    events: list[tuple[str, dict]] = []
    engine._emit_event_callback = lambda n, d: events.append((n, d))
    curve = _make_predicted_indoor(start_hour_utc=10, temps=curve_temps)
    for i in range(cycles):
        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = _NOW.replace(hour=10 + (i // 2), minute=30 * (i % 2))
            asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve))
    return engine, [d for n, d in events if n == "ceiling_guard_fired"]


def _guard_calls(engine) -> list[tuple[str, object]]:
    """The calls made after the comfort band's own write: everything after the first set_temperature."""
    calls = engine.hass.services.async_call.call_args_list
    first = next((i for i, c in enumerate(calls) if c.args[1] == "set_temperature"), None)
    if first is None:
        return []
    return [(c.args[1], c.args[2].get("hvac_mode") or c.args[2].get("temperature")) for c in calls[first + 1 :]]


@pytest.mark.parametrize("setpoint", [74.0, 72.0, 70.0])
def test_a_thermostat_cooling_at_or_below_the_ceiling_is_left_alone(setpoint: float) -> None:
    """Awake band (74), sleep band (72) or anything colder: the guard never raises a cool setpoint."""
    engine, fired = _run(mode="cool", setpoint=setpoint)
    assert fired == [], "no 'ceiling_guard_fired' Activity row for a write that was not needed"
    assert _guard_calls(engine) == []
    assert not any(kind == "set_hvac_mode" for kind, _v in _guard_calls(engine)), "no second mode write"
    assert engine._comfort_mode_family == "cooling"


def test_the_sleep_band_72_is_never_raised_to_the_awake_ceiling_74() -> None:
    engine, fired = _run(mode="cool", setpoint=72.0, cycles=6)
    assert fired == []
    # one setpoint write per cycle: the comfort band's own; the guard adds none after it
    temps = [c for c in engine.hass.services.async_call.call_args_list if c.args[1] == "set_temperature"]
    assert len(temps) == 6


def test_six_cycles_in_a_row_never_re_send_after_the_old_two_hour_window() -> None:
    """The old rule re-sent when its 7200 s window expired; the state test has no clock."""
    engine, fired = _run(mode="cool", setpoint=74.0, cycles=6)
    assert fired == []
    guard_mode_writes = [c for c in engine.hass.services.async_call.call_args_list if c.args[1] == "set_hvac_mode"]
    assert not [c for c in guard_mode_writes if c.args[2].get("hvac_mode") == "cool"]


@pytest.mark.parametrize(("mode", "setpoint"), [("off", None), ("cool", 76.0)])
def test_the_guard_still_acts_when_off_or_cooling_above_the_ceiling(mode: str, setpoint) -> None:
    """The backstop role is unchanged: a lapsed band (off) or a cool setpoint ABOVE the ceiling gets 74 F."""
    engine, fired = _run(mode=mode, setpoint=setpoint)
    assert len(fired) == 1
    assert fired[0]["new_setpoint_f"] == 74.0 and fired[0]["old_hvac_mode"] == mode
    assert ("set_temperature", 74.0) in _guard_calls(engine) or any(
        c.args[1] == "set_temperature" and c.args[2].get("temperature") == 74.0
        for c in engine.hass.services.async_call.call_args_list
    )


def test_observe_only_still_records_what_it_would_have_done_for_an_off_thermostat() -> None:
    engine, fired = _run(mode="off", setpoint=None, dry_run=True)
    assert fired == [] and not engine.hass.services.async_call.call_args_list  # nothing sent, nothing recorded


@pytest.mark.parametrize(
    ("curve", "outcome"),
    [(_BREACH, OdeCeilingGuardOutcome.ESCALATE), (_CALM, OdeCeilingGuardOutcome.NO_BREACH_PREDICTED)],
)
def test_the_guard_reads_whichever_curve_the_decisions_are_handed(curve, outcome) -> None:
    """Primary dependency (demoted cards 1-4): the curve is the only thing that differs between v3 and v4 primary."""
    engine = _make_cg_engine(comfort_cool=74.0, indoor_temp=73.0)
    engine._last_outdoor_temp = 76.0
    _set_thermal_model(engine, k_passive=-0.05, conf="medium", k_active_cool=None)
    with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
        mock_dt.now.return_value = _NOW
        decision = engine._resolve_classification_fsm_state(
            classification=_make_warm_off_classification(),
            predicted_indoor=_make_predicted_indoor(start_hour_utc=10, temps=curve),
        )
    assert decision.ceiling_decision.outcome is outcome


# ---------------------------------------------------------------- the duplicate comfort-band write (same tick)
def _band_engine(mode: str = "cool", setpoint: float = 72.0):
    engine = _make_cg_engine(comfort_cool=74.0, indoor_temp=73.0)
    engine.hass.states.get.return_value.state = mode
    engine.hass.states.get.return_value.attributes["temperature"] = setpoint
    return engine


def _arm_twice(engine, *, gap_s: float = 1.0, between=None):
    from custom_components.climate_advisor.automation import ComfortBand

    band = ComfortBand(floor=66.0, ceiling=72.0, active="ceiling", reason="sleep band", context="sleep")
    for i in range(2):
        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = _NOW.replace(second=0) + (timedelta(seconds=gap_s) * i)
            asyncio.run(engine._apply_comfort_band(band, reason="bedtime" if i == 0 else "daily classification"))
        if i == 0 and between:
            between()
    return [c for c in engine.hass.services.async_call.call_args_list if c.args[1] == "set_temperature"]


def test_bedtime_band_then_the_same_half_hours_re_arm_writes_twice() -> None:
    """22:30: bedtime arms the sleep band 72 and the classification re-arm a second later writes it again. The
    thermostat command is never deduped (owner-confirmed Issue #590 Finding D); a same-moment skip was tried in
    #1230 R6 and reverted because it overrides that decision and broke the automation-on positive control."""
    writes = _arm_twice(_band_engine())
    assert [w.args[2]["temperature"] for w in writes] == [72.0, 72.0]


def test_the_same_band_a_cycle_later_is_still_re_asserted() -> None:
    """The scheduled re-assertion is deliberate (Issue #590 Finding D)."""
    writes = _arm_twice(_band_engine(), gap_s=1800)
    assert len(writes) == 2


def test_a_thermostat_that_no_longer_holds_the_band_is_written_again() -> None:
    engine = _band_engine()

    def occupant_changes():
        engine.hass.states.get.return_value.attributes["temperature"] = 75.0

    assert len(_arm_twice(engine, between=occupant_changes)) == 2


# --- Issue #1403: CA's own setpoint reported a fraction above the ceiling (the #1397 hold tolerance) ---------------
#
# Occupant view: a thermostat that reports CA's 74 F write as 74.3 F (rounding, or 0.5 C steps) is still holding CA's
# setpoint. Before #1403 the guard read that as "cooling above the ceiling" and re-sent cool + 74 every cycle. An
# occupant's own setpoint above the ceiling (not CA's write, or outside the 0.5 F tolerance) still gets the guard.


@pytest.mark.parametrize("reported", [74.1, 74.3, 74.5])
def test_cas_own_74_reported_a_fraction_high_is_left_alone(reported: float) -> None:
    """The comfort band re-sends 74 earlier in the cycle (in flight); the thermostat still shows 74.x: guard quiet."""
    engine, fired = _run(mode="cool", setpoint=reported, cycles=3)
    assert fired == [], f"CA's own 74 reported as {reported} is not 'cooling above the ceiling'"
    assert not [
        c
        for c in engine.hass.services.async_call.call_args_list
        if c.args[1] == "set_hvac_mode" and c.args[2].get("hvac_mode") == "cool"
    ], "no guard mode write"


def _run_without_in_flight(*, setpoint: float, held: dict | None):
    """One cycle where CA's band write is NOT in the echo window (only the held record, if any, says what CA wrote)."""
    engine: AutomationEngine = _make_cg_engine(comfort_cool=74.0, indoor_temp=73.0)
    engine.hass.states.get.return_value.state = "cool"
    engine.hass.states.get.return_value.attributes["temperature"] = setpoint
    engine._last_outdoor_temp = 76.0
    _set_thermal_model(engine, k_passive=-0.05, conf="medium", k_active_cool=None)
    events: list[tuple[str, dict]] = []
    engine._emit_event_callback = lambda n, d: events.append((n, d))
    engine._remember_own_setpoint_write = lambda *a, **k: None  # nothing of CA's in flight, held record untouched
    engine._own_setpoint_writes = []
    engine._held_own_setpoints = {engine.climate_entity: held} if held else {}
    curve = _make_predicted_indoor(start_hour_utc=10, temps=_BREACH)
    with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
        mock_dt.now.return_value = _NOW.replace(hour=10, minute=30)
        asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve))
    return [d for n, d in events if n == "ceiling_guard_fired"]


def test_a_held_own_74_with_nothing_in_flight_is_left_alone() -> None:
    """The thermostat showed CA's 74 earlier (held) and now reports 74.3, nothing in the echo window: guard quiet."""
    assert _run_without_in_flight(setpoint=74.3, held={"temperature": 74.0}) == []


def test_a_held_own_write_above_the_ceiling_does_not_silence_the_guard() -> None:
    """Only CA's write AT or below the ceiling counts: a held 76 (e.g. a setback) reported as 76.2 still gets 74."""
    fired = _run_without_in_flight(setpoint=76.2, held={"temperature": 76.0})
    assert len(fired) == 1 and fired[0]["new_setpoint_f"] == 74.0


@pytest.mark.parametrize("reported", [74.6, 76.0])
def test_a_setpoint_beyond_the_tolerance_above_the_ceiling_still_gets_the_guard(reported: float) -> None:
    """74.6 is more than 0.5 F from CA's 74: that is not CA's setpoint, so the backstop acts as before."""
    engine, fired = _run(mode="cool", setpoint=reported)
    assert len(fired) == 1 and fired[0]["new_setpoint_f"] == 74.0


def test_an_occupant_setpoint_above_the_ceiling_with_no_own_write_still_gets_the_guard() -> None:
    """No CA write held or in flight: 74.3 is the occupant's, so the guard acts as before."""
    fired = _run_without_in_flight(setpoint=74.3, held=None)
    assert len(fired) == 1 and fired[0]["new_setpoint_f"] == 74.0


def test_the_celsius_tolerance_covers_a_half_degree_step_report() -> None:
    """23.3 C written, 23.5 C reported (0.5 C-step thermostat): within 0.28 C, so it is CA's setpoint (#1397 rule)."""
    from custom_components.climate_advisor.own_write_echo import hold_tolerance, holds_own_setpoint

    assert holds_own_setpoint(
        held={"temperature": 23.3}, observed={"temperature": 23.5}, tolerance=hold_tolerance("celsius")
    )
    assert not holds_own_setpoint(
        held={"temperature": 23.3}, observed={"temperature": 23.7}, tolerance=hold_tolerance("celsius")
    )
