"""Issue #1245: the arrival-home restore event reports what the thermostat was actually sent.

Occupant framing: you come home and the Activity Report says "comfort restored to 74" while the thermostat was
sent the sleep band's 70 (or aggressive savings' wider edge) -- or while a whole-house-fan session or a blower
ventilating through an open window correctly kept HVAC Off and nothing was written at all.

Real ``AutomationEngine`` + the state-following fake thermostat (same harness as ``test_return_home_off_day_1208``).
Assertions are on the setpoint the thermostat received and the events emitted; no production logic re-implemented.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.climate_advisor.ai_skills_context import EVENT_RENDERERS
from custom_components.climate_advisor.automation import HvacCommandResult
from custom_components.climate_advisor.const import FAN_MODE_HVAC, FAN_MODE_WHOLE_HOUSE
from tests.test_promise_checks import _cold_house, events, hits_for, make_input
from tests.test_return_home_off_day_1208 import (
    _COMFORT,
    _FANS,
    _engine_for,
    _leave_and_return,
    _run_at,
    _set_indoor,
    _setpoints,
)
from tools.sim_harness.outcomes import ProductionDecision, _map_event_to_outcome, production_outcome_at

_DAYS = ["hot", "warm", "mild", "cold"]


def _events_of(engine, kind):
    return [d for k, d in engine._events if k == kind]


def _arrive_held_by_whf(day, indoor=None):
    engine = _engine_for(day, FAN_MODE_WHOLE_HOUSE)
    _run_at(engine, engine.handle_occupancy_away)
    engine._clock.advance(hours=4)
    engine._natural_vent_active = True
    engine._fan_active = True
    engine._pre_fan_hvac_mode = "cool"  # a WHF suppression session is live
    engine._test_state.state = "off"
    if indoor is not None:
        _set_indoor(engine, indoor)
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, engine.handle_occupancy_home)
    return engine, mark


def _arrive_held_by_blower(day, indoor=None):
    engine = _engine_for(day, FAN_MODE_HVAC)
    _run_at(engine, engine.handle_occupancy_away)
    engine._clock.advance(hours=4)
    engine._natural_vent_active = True
    engine._sensor_check_callback = lambda: True
    engine._sensor_debounce_pending_callback = lambda: False
    engine._test_state.state = "off"
    if indoor is not None:
        _set_indoor(engine, indoor)
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, engine.handle_occupancy_home)
    return engine, mark


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("day", _DAYS)
def test_normal_arrival_reports_the_written_edge(day, fan):
    expected_mode, expected_temp, _ = _COMFORT[day]
    engine = _engine_for(day, fan)
    mark = _leave_and_return(engine)
    assert _setpoints(engine, mark) == [(expected_mode, expected_temp)]
    restored = _events_of(engine, "occupancy_comfort_restored")
    assert len(restored) == 1
    assert (restored[0]["mode"], restored[0]["target_f"]) == (expected_mode, expected_temp)
    assert restored[0]["outcome"] == "sent"
    assert _events_of(engine, "occupancy_comfort_restore_held") == []


@pytest.mark.parametrize("fan", _FANS)
def test_sleep_band_arrival_reports_the_sleep_edge_not_the_flat_comfort_setting(fan):
    engine = _engine_for("warm", fan, sleep_time="22:00", wake_time="07:00", sleep_heat=64.0, sleep_cool=70.0)
    engine._clock.now = engine._clock.now.replace(hour=23)
    mark = _leave_and_return(engine)
    written = _setpoints(engine, mark)
    assert written == [("cool", 70.0)], "precondition: the sleep ceiling (not the flat 74) is what is written"
    restored = _events_of(engine, "occupancy_comfort_restored")
    assert len(restored) == 1 and restored[0]["target_f"] == 70.0 and restored[0]["mode"] == "cool"


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("day", ["hot", "warm"])
def test_aggressive_savings_arrival_reports_the_widened_edge(day, fan):
    engine = _engine_for(day, fan, aggressive_savings=True)
    mark = _leave_and_return(engine)
    written = _setpoints(engine, mark)
    assert len(written) == 1
    restored = _events_of(engine, "occupancy_comfort_restored")
    assert len(restored) == 1
    assert restored[0]["target_f"] == written[0][1] and restored[0]["mode"] == written[0][0]
    assert written[0][1] != _COMFORT[day][1], "precondition: aggressive savings moved the edge off the flat setting"


@pytest.mark.parametrize("day", _DAYS)
def test_whf_session_arrival_emits_held_not_restored(day):
    engine, mark = _arrive_held_by_whf(day)
    assert [w for w in _setpoints(engine, mark) if w[0] in ("heat", "cool")] == []
    assert _events_of(engine, "occupancy_comfort_restored") == []
    held = _events_of(engine, "occupancy_comfort_restore_held")
    assert len(held) == 1 and held[0]["reason"] == "whf_session"


@pytest.mark.parametrize("day", _DAYS)
def test_blower_hold_arrival_emits_held_not_restored(day):
    engine, mark = _arrive_held_by_blower(day)
    assert [w for w in _setpoints(engine, mark) if w[0] in ("heat", "cool")] == []
    assert _events_of(engine, "occupancy_comfort_restored") == []
    held = _events_of(engine, "occupancy_comfort_restore_held")
    assert len(held) == 1 and held[0]["reason"] == "blower_hold"


def test_held_event_is_windowed_deduped():
    engine, _mark = _arrive_held_by_blower("warm")
    _run_at(engine, engine.handle_occupancy_home)
    assert len(_events_of(engine, "occupancy_comfort_restore_held")) == 1


@pytest.mark.parametrize("arrive", [_arrive_held_by_whf, _arrive_held_by_blower])
def test_welcome_home_push_does_not_promise_a_restore_that_was_held(arrive):
    # indoor 79 F is nearer the 80 F setback than the 74 F comfort, so the push is not near-comfort suppressed.
    engine, _mark = arrive("hot", indoor=79.0)
    assert engine._notes, "precondition: a welcome-home push was sent"
    assert not any("Restoring comfort temperature" in m for m in engine._notes)
    assert any("resume once ventilation ends" in m for m in engine._notes)


# ---------------------------------------------------------------------------
# Verify pass: an attempted-but-not-sent restore, the P7 away timeline, the harness mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan", _FANS)
def test_dry_run_arrival_is_a_restore_whose_write_was_not_sent(fan):
    """Test mode: CA decided the restore (still occupancy_comfort_restored) but the report must not claim a setpoint."""
    engine = _engine_for("warm", fan)
    _run_at(engine, engine.handle_occupancy_away)
    engine._clock.advance(hours=4)
    engine.dry_run = True
    _run_at(engine, engine.handle_occupancy_home)
    (restored,) = _events_of(engine, "occupancy_comfort_restored")
    assert restored["outcome"] == "dry_run"
    label, _settings = EVENT_RENDERERS["occupancy_comfort_restored"](restored, "F")
    assert "NOT sent" in label and "comfort restored" not in label


def test_failed_arrival_write_is_reported_failed_not_restored():
    engine = _engine_for("warm", FAN_MODE_HVAC)
    _run_at(engine, engine.handle_occupancy_away)
    engine._clock.advance(hours=4)
    engine._set_temperature = AsyncMock(return_value=HvacCommandResult.FAILED)
    _run_at(engine, engine.handle_occupancy_home)
    (restored,) = _events_of(engine, "occupancy_comfort_restored")
    assert restored["outcome"] == "failed"
    label, _settings = EVENT_RENDERERS["occupancy_comfort_restored"](restored, "F")
    assert "NOT sent" in label and "command failed" in label


def test_historical_restored_row_without_outcome_keeps_its_wording():
    label, settings = EVENT_RENDERERS["occupancy_comfort_restored"]({"mode": "cool", "target_f": 74}, "F")
    assert label == "Occupancy -- comfort restored (cool)" and "74" in settings


def test_held_arrival_ends_the_p7_away_timeline():
    """The occupant is home; once ventilation ends the house must be watched again (P7), not stay 'away' forever."""
    held = events((-30, "occupancy_setback", {}), (-20, "occupancy_comfort_restore_held", {"reason": "whf_session"}))
    assert len(hits_for("comfort_kept", make_input(_cold_house(60), marks=held))) == 1
    still_away = events((-30, "occupancy_setback", {}))
    assert hits_for("comfort_kept", make_input(_cold_house(60), marks=still_away)) == []


def test_held_event_does_not_shadow_the_outcome_in_force_in_the_harness():
    """A held arrival during ventilation: the outcome in force stays natural_ventilation, not 'unknown:<type>'."""
    rows = [
        ProductionDecision("2026-07-15T18:00:00", "nat_vent_reactivated_while_paused", "natural_ventilation"),
        _map_event_to_outcome("occupancy_comfort_restore_held", {"reason": "whf_session"}, "2026-07-15T18:05:00"),
    ]
    assert production_outcome_at([r for r in rows if r is not None], "2026-07-15T18:06:00") == "natural_ventilation"
