"""Issues #1267 / #1596: the setback, bedtime and wake-up rows report what the thermostat was actually sent.

Occupant framing: you leave home (or go to bed, or wake up) while a whole-house fan session owns the thermostat,
while the blower ventilates through an open window, in test mode, or when the thermostat rejects the command. The
Activity Report and the AI context said "Occupancy setback -- away" / "Bedtime setback" / "Morning wake-up" with the
new band although the thermostat was not changed. The row is now emitted after the write and carries ``outcome``
(and ``held_reason`` when nothing was written); the renderer says "NOT sent" with the reason.

Real ``AutomationEngine`` + the state-following fake thermostat (same harness as ``test_restore_event_1245``).
Assertions are on the setpoints the thermostat received and the rows emitted; no production logic re-implemented.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.climate_advisor.ai_skills_context import EVENT_RENDERERS
from custom_components.climate_advisor.automation import HvacCommandResult
from custom_components.climate_advisor.const import FAN_MODE_HVAC, FAN_MODE_WHOLE_HOUSE
from tests.test_promise_checks import _cold_house, events, hits_for, make_input
from tests.test_return_home_off_day_1208 import _FANS, _engine_for, _run_at, _setpoints

_DAYS = ["hot", "warm", "mild", "cold"]
_ROWS = ["occupancy_setback", "bedtime_setback", "morning_wakeup"]


def _rows(engine, kind):
    return [d for k, d in engine._events if k == kind]


def _trigger(engine, kind):
    """Run the routine that emits ``kind`` (1 h after the classification, past the 30 s settle guard)."""
    engine._clock.advance(hours=1)
    handler = {
        "occupancy_setback": engine.handle_occupancy_away,
        "bedtime_setback": engine.handle_bedtime,
        "morning_wakeup": engine.handle_morning_wakeup,
    }[kind]
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, handler)
    return mark


def _whf_session(engine):
    engine._natural_vent_active = True
    engine._fan_active = True
    engine._pre_fan_hvac_mode = "cool"  # a WHF suppression session owns the thermostat
    engine._test_state.state = "off"


def _blower_session(engine):
    engine._natural_vent_active = True
    engine._sensor_check_callback = lambda: True
    engine._sensor_debounce_pending_callback = lambda: False
    engine._test_state.state = "off"


def _record_emit_order(engine):
    """Wrap the event callback: remember how many service calls had been made when each row was emitted."""
    seen: list[tuple[str, dict, int]] = []
    inner = engine._emit_event_callback

    def _emit(kind, data):
        seen.append((kind, data, len(engine.hass.services.async_call.call_args_list)))
        inner(kind, data)

    engine._emit_event_callback = _emit
    return seen


# ---------------------------------------------------------------------------
# 1. Sent: every day type, both fan modes; the row follows the write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", _ROWS)
@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("day", _DAYS)
def test_sent_band_row_says_sent_and_follows_the_write(day, fan, kind):
    engine = _engine_for(day, fan)
    seen = _record_emit_order(engine)
    mark = _trigger(engine, kind)
    written = _setpoints(engine, mark)
    assert written, f"precondition: {kind} wrote the band"
    (row,) = _rows(engine, kind)
    assert row["outcome"] == "sent"
    assert "held_reason" not in row
    (calls_at_emit,) = [n for k, _d, n in seen if k == kind]
    assert calls_at_emit > mark, f"{kind} was emitted before the band write"
    label, _settings = EVENT_RENDERERS[kind](row, "F")
    assert "NOT sent" not in label


# ---------------------------------------------------------------------------
# 2. Not sent: WHF session, blower hold, test mode, thermostat rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", _ROWS)
@pytest.mark.parametrize("day", _DAYS)
def test_whf_session_band_row_is_blocked_not_sent(day, kind):
    engine = _engine_for(day, FAN_MODE_WHOLE_HOUSE)
    _whf_session(engine)
    mark = _trigger(engine, kind)
    assert [w for w in _setpoints(engine, mark) if w[0] in ("heat", "cool")] == []
    (row,) = _rows(engine, kind)
    assert (row["outcome"], row["held_reason"]) == ("blocked", "whf_session")
    label, _settings = EVENT_RENDERERS[kind](row, "F")
    assert "NOT sent" in label and "whole-house fan running" in label


@pytest.mark.parametrize("kind", _ROWS)
@pytest.mark.parametrize("day", _DAYS)
def test_blower_hold_band_row_is_held_not_sent(day, kind):
    engine = _engine_for(day, FAN_MODE_HVAC)
    _blower_session(engine)
    mark = _trigger(engine, kind)
    assert [w for w in _setpoints(engine, mark) if w[0] in ("heat", "cool")] == []
    (row,) = _rows(engine, kind)
    assert (row["outcome"], row["held_reason"]) == ("not_sent", "blower_hold")
    label, _settings = EVENT_RENDERERS[kind](row, "F")
    assert "NOT sent" in label and "window open, blower ventilating" in label


@pytest.mark.parametrize("kind", _ROWS)
@pytest.mark.parametrize("fan", _FANS)
def test_dry_run_band_row_says_test_mode(fan, kind):
    engine = _engine_for("warm", fan)
    engine.dry_run = True
    _trigger(engine, kind)
    (row,) = _rows(engine, kind)
    assert row["outcome"] == "dry_run"
    label, _settings = EVENT_RENDERERS[kind](row, "F")
    assert "NOT sent" in label and "test mode" in label


@pytest.mark.parametrize("kind", _ROWS)
@pytest.mark.parametrize("fan", _FANS)
def test_failed_band_row_says_command_failed(fan, kind):
    engine = _engine_for("mild", fan)
    engine._set_temperature = AsyncMock(return_value=HvacCommandResult.FAILED)
    _trigger(engine, kind)
    (row,) = _rows(engine, kind)
    assert row["outcome"] == "failed"
    label, _settings = EVENT_RENDERERS[kind](row, "F")
    assert "NOT sent" in label and "command failed" in label


@pytest.mark.parametrize("fan", _FANS)
def test_band_write_with_a_window_open_and_no_session_is_held_window_open(fan):
    """The choke point pauses instead of arming (Issue #629, the last line of defence when the routines' own pause
    sync lagged): the record every band row reads says the band was not sent, and why."""
    engine = _engine_for("warm", fan)
    engine._sensor_check_callback = lambda: True
    engine._sensor_debounce_pending_callback = lambda: False
    engine._clock.advance(hours=1)
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, lambda: engine._apply_comfort_band(engine._last_comfort_band, reason="test"))
    assert [w for w in _setpoints(engine, mark) if w[0] in ("heat", "cool")] == []
    assert engine._last_band_write.held_reason == "window_open"
    label, _settings = EVENT_RENDERERS["bedtime_setback"](
        {
            "mode": "off",
            "floor": 64,
            "ceiling": 72,
            "active": "ceiling",
            "outcome": "not_sent",
            "held_reason": "window_open",
        },
        "F",
    )
    assert "NOT sent" in label and "window open, HVAC paused" in label


@pytest.mark.parametrize("kind", _ROWS)
def test_no_capable_mode_band_row_is_held(kind):
    """A thermostat that advertises neither heat nor cool cannot serve the band: nothing written, the row says so."""
    engine = _engine_for("hot", FAN_MODE_HVAC)
    engine._test_state.attributes["hvac_modes"] = ["off", "fan_only"]
    mark = _trigger(engine, kind)
    assert [w for w in _setpoints(engine, mark) if w[0] in ("heat", "cool")] == []
    (row,) = _rows(engine, kind)
    assert (row["outcome"], row["held_reason"]) == ("not_sent", "no_capable_mode")
    label, _settings = EVENT_RENDERERS[kind](row, "F")
    assert "NOT sent" in label and "thermostat has no mode for this edge" in label


# ---------------------------------------------------------------------------
# 3. Dedup signature: a held row followed by a sent one inside 600 s is two rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["occupancy_setback", "morning_wakeup"])
def test_blocked_then_sent_within_the_dedup_window_gives_two_rows(kind):
    engine = _engine_for("warm", FAN_MODE_WHOLE_HOUSE)
    _whf_session(engine)
    _trigger(engine, kind)
    engine._natural_vent_active = False
    engine._fan_active = False
    engine._pre_fan_hvac_mode = None  # the session ended; the same band is now written
    engine._clock.advance(minutes=2)
    mark = len(engine.hass.services.async_call.call_args_list)
    handler = engine.handle_occupancy_away if kind == "occupancy_setback" else engine.handle_morning_wakeup
    _run_at(engine, handler)
    assert _setpoints(engine, mark), "precondition: the band was written the second time"
    assert [r["outcome"] for r in _rows(engine, kind)] == ["blocked", "sent"]


def test_identical_sent_setback_inside_the_window_is_still_deduped():
    engine = _engine_for("warm", FAN_MODE_HVAC)
    _trigger(engine, "occupancy_setback")
    engine._clock.advance(minutes=2)
    _run_at(engine, engine.handle_occupancy_away)
    assert len(_rows(engine, "occupancy_setback")) == 1


# ---------------------------------------------------------------------------
# 4. P7 away timeline and old rows
# ---------------------------------------------------------------------------


def test_not_sent_setback_still_starts_the_p7_away_timeline():
    """The occupant did leave: a held setback row keeps the P7 'away' meaning (same event type)."""
    engine = _engine_for("cold", FAN_MODE_WHOLE_HOUSE)
    _whf_session(engine)
    _trigger(engine, "occupancy_setback")
    (row,) = _rows(engine, "occupancy_setback")
    assert row["outcome"] == "blocked"
    away = events((-30, "occupancy_setback", row))
    assert hits_for("comfort_kept", make_input(_cold_house(60), marks=away)) == []
    home = events((-30, "occupancy_setback", row), (-20, "occupancy_comfort_restored", {"outcome": "sent"}))
    assert len(hits_for("comfort_kept", make_input(_cold_house(60), marks=home))) == 1


@pytest.mark.parametrize(
    ("kind", "payload", "expected"),
    [
        ("occupancy_setback", {"mode": "away", "occupancy": "away", "floor": 62, "ceiling": 80}, "Occupancy setback"),
        ("bedtime_setback", {"mode": "cool", "floor": 64, "ceiling": 78, "active": "ceiling"}, "Bedtime setback"),
        ("morning_wakeup", {"mode": "cool", "floor": 68, "ceiling": 74, "active": "ceiling"}, "Morning wake-up"),
    ],
)
def test_historical_rows_without_outcome_keep_their_wording(kind, payload, expected):
    label, settings = EVENT_RENDERERS[kind](payload, "F")
    assert label.startswith(expected) and "NOT sent" not in label
    assert settings


def test_tou_bank_setback_row_not_sent_says_so():
    payload = {
        "mode": "away",
        "occupancy": "away",
        "floor": 62,
        "ceiling": 80,
        "tou_bank": True,
        "target_f": 72,
        "hvac_mode": "cool",
        "outcome": "failed",
    }
    label, _settings = EVENT_RENDERERS["occupancy_setback"](payload, "F")
    assert "NOT sent" in label and "command failed" in label
