"""The ODE ceiling guard sends nothing when the thermostat already cools at or below its ceiling (Issue #1230 R6, X1).

Occupant view: on a warm "off" day the thermostat is already cooling at 74 F (the comfort band armed it). With the
primary engine's no-AC forecast (v4) the breach is predicted on every cycle, and the guard used to re-send "cool" plus
the same setpoint every two hours and, from bedtime, write 74 F under the 72 F sleep band each half hour (the bedroom
held at 74). The guard now acts only when the thermostat is off, in heat, or cooling ABOVE the ceiling; one rule for v3
and v4. These are the unit-level twins of ``r6b_warm_off_day_no_redundant_guard_writes__v3/__v4`` (golden drafts) and the
demoted cards 1-5 (the guard can never be the writer of the 12:30 cool when the band already arms it).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
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


@pytest.mark.parametrize(("curve", "outcome"), [(_BREACH, OdeCeilingGuardOutcome.ESCALATE), (_CALM, OdeCeilingGuardOutcome.NO_BREACH_PREDICTED)])
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
