"""Issue #1371 (and #1376): overnight pre-cool fires and holds only at night.

Occupant view:
- On a warm or mild day with a warming trend, the "overnight" pre-cool used to fire at 10:30 AM or 5:30 PM. The
  window-close time plus 30 minutes landed in the day, so the thermostat flipped cool -> heat -> cool while the
  house sat comfortably inside the band. It now fires only in the sleep window.
- On a hot-trend night the pre-cool lowered the cool ceiling, and the 30-minute cycle put the plain sleep
  ceiling back in the same second, so no cold mass was ever banked. It now holds until wake time.
- A pre-cool's cool command now counts as deliberate for the settling guard, as the switching spec says (R3).
- #1376: a bedtime after midnight (01:00 -> 07:00) no longer makes the whole day count as sleep hours.

Every test here fails on the pre-fix code.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor.automation import _in_sleep_window
from custom_components.climate_advisor.coordinator import _compute_pre_cool_trigger_time_pure
from tests.test_pre_cool import _make_classification, _make_engine


def _clock(monkeypatch, at: datetime) -> list[datetime]:
    """Pin the stubbed HA clock (default zone UTC) to ``at``; returns a one-item box to move it."""
    box = [at]
    dt_mod = sys.modules["homeassistant.util.dt"]
    monkeypatch.setattr(dt_mod, "now", lambda time_zone=None: box[0])
    monkeypatch.setattr(dt_mod, "utcnow", lambda: box[0])
    return box


# ---------------------------------------------------------------------------
# #1376: the sleep window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sleep", "wake", "at", "asleep"),
    [
        ("22:30", "07:00", time(23, 0), True),
        ("22:30", "07:00", time(3, 0), True),
        ("22:30", "07:00", time(22, 30), True),
        ("22:30", "07:00", time(7, 0), False),
        ("22:30", "07:00", time(12, 0), False),
        # bedtime after midnight: no wrap
        ("01:00", "07:00", time(0, 30), False),
        ("01:00", "07:00", time(1, 0), True),
        ("01:00", "07:00", time(6, 59), True),
        ("01:00", "07:00", time(12, 0), False),  # was True before #1376: the whole day counted as asleep
        ("01:00", "07:00", time(23, 0), False),  # was True before #1376
        ("22:00", "22:00", time(12, 0), False),  # equal times: empty window
    ],
)
def test_sleep_window(sleep, wake, at, asleep):
    now = datetime.combine(datetime(2026, 7, 1).date(), at, tzinfo=UTC)
    assert _in_sleep_window(now, {"sleep_time": sleep, "wake_time": wake}) is asleep


# ---------------------------------------------------------------------------
# #1371 step 1: the trigger lands in the sleep window
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
_TONIGHT_0230 = datetime(2026, 7, 2, 2, 30, tzinfo=UTC)


def _trigger(window_close, **config):
    c = SimpleNamespace(setback_modifier=-3.0, tomorrow_high=70.0, window_close_time=window_close)
    cfg = {"comfort_cool": 74.0, "sleep_cool": 78.0, "sleep_time": "22:30", "wake_time": "06:30", **config}
    return _compute_pre_cool_trigger_time_pure(c, cfg, _NOW)


@pytest.mark.parametrize(
    ("label", "window_close", "config", "expected"),
    [
        ("warm day: close 10:00 is daytime -> overnight", time(10, 0), {}, _TONIGHT_0230),  # was 10:30 today
        ("mild day: close 17:00 is daytime -> overnight", time(17, 0), {}, _TONIGHT_0230),  # was 17:30 today
        ("close 23:00 inside the sleep window", time(23, 0), {}, datetime(2026, 7, 1, 23, 30, tzinfo=UTC)),
        ("hot day: no close time -> wake - 4 h", None, {}, _TONIGHT_0230),
        (
            "short night 03:00 -> 05:00: never before bedtime",
            None,
            {"sleep_time": "03:00", "wake_time": "05:00"},
            datetime(2026, 7, 2, 3, 0, tzinfo=UTC),
        ),
        ("no sleep/wake configured: pre-cool defaults", time(10, 0), {"sleep_time": None, "wake_time": None}, None),
    ],
)
def test_trigger_is_overnight(label, window_close, config, expected):
    if expected is None:  # defaults 22:30 / 06:30 -> 02:30 tonight
        expected = _TONIGHT_0230
    cfg = {k: v for k, v in config.items() if v is not None}
    if "sleep_time" in config and config["sleep_time"] is None:
        c = SimpleNamespace(setback_modifier=-3.0, tomorrow_high=70.0, window_close_time=window_close)
        got = _compute_pre_cool_trigger_time_pure(c, {"comfort_cool": 74.0, "sleep_cool": 78.0}, _NOW)
    else:
        got = _trigger(window_close, **cfg)
    assert got == expected, label


# ---------------------------------------------------------------------------
# #1371 step 1 backstop: the handler refuses during awake hours
# ---------------------------------------------------------------------------


def test_handler_skips_during_awake_hours(monkeypatch):
    _clock(monkeypatch, datetime(2026, 7, 1, 10, 30, tzinfo=UTC))
    engine = _make_engine({"sleep_cool": 72.0})
    engine._current_classification = _make_classification(window_close_time=time(10, 0))
    result = asyncio.run(engine.handle_pre_cool(indoor_temp=69.5, nat_vent_just_closed=False))
    assert result == "skipped: awake hours"
    assert engine.hass.services.async_call.await_count == 0


def test_handler_fires_at_night(monkeypatch):
    _clock(monkeypatch, datetime(2026, 7, 2, 2, 30, tzinfo=UTC))
    engine = _make_engine({"sleep_cool": 72.0})
    engine._current_classification = _make_classification()
    result = asyncio.run(engine.handle_pre_cool(indoor_temp=76.0, nat_vent_just_closed=False))
    assert result.startswith("applied"), result


# ---------------------------------------------------------------------------
# #1371 step 2: the pre-cool holds, and counts as a deliberate cool command
# ---------------------------------------------------------------------------


def _cool_writes(engine) -> list[float]:
    out = []
    for call in engine.hass.services.async_call.await_args_list:
        data = call.args[2] if len(call.args) > 2 else call.kwargs.get("service_data", {})
        if call.args[1] == "set_temperature" and data.get("hvac_mode") == "cool":
            out.append(float(data["temperature"]))
    return out


def _pre_cooled_engine(monkeypatch, *, from_mode: str = "cool"):
    box = _clock(monkeypatch, datetime(2026, 7, 2, 2, 30, tzinfo=UTC))
    engine = _make_engine({"sleep_cool": 72.0, "sleep_heat": 64.0})
    engine.hass.states.get.return_value.state = from_mode
    engine.hass.states.get.return_value.attributes = {
        **engine.hass.states.get.return_value.attributes,
        "current_temperature": 76.0,
    }
    c = _make_classification()
    engine._current_classification = c
    assert asyncio.run(engine.handle_pre_cool(indoor_temp=76.0, nat_vent_just_closed=False)).startswith("applied")
    target = engine._pre_cool_band_in_force.ceiling
    assert target < 72.0
    return engine, c, box, target


def test_cycle_keeps_the_pre_cool_until_wake(monkeypatch):
    engine, c, box, target = _pre_cooled_engine(monkeypatch)
    engine.hass.services.async_call.reset_mock()

    box[0] = datetime(2026, 7, 2, 3, 0, tzinfo=UTC)
    asyncio.run(engine.apply_classification(c))
    assert _cool_writes(engine) == [target], "the 30-min cycle must keep the pre-cool ceiling, not sleep_cool 72"
    assert engine._pre_cool_band_in_force is not None, "re-sending the pre-cool ceiling keeps the hold"
    assert engine._resolved_comfort_band(c).ceiling == target, "readers and restores see the held band too"


def test_hold_ends_at_wake(monkeypatch):
    engine, c, box, target = _pre_cooled_engine(monkeypatch)
    box[0] = datetime(2026, 7, 2, 6, 45, tzinfo=UTC)
    assert engine._pre_cool_band_in_force_now() is None
    assert engine._resolved_comfort_band(c).ceiling != target


def test_a_different_write_ends_the_hold(monkeypatch):
    engine, _c, _box, _target = _pre_cooled_engine(monkeypatch)
    asyncio.run(engine._set_temperature(64.0, reason="floor breach", mode="heat"))
    assert engine._pre_cool_band_in_force is None


def test_pre_cool_write_is_a_deliberate_cool_for_the_settling_guard(monkeypatch):
    from custom_components.climate_advisor.automation import _DELIBERATE_MODE_REASONS

    engine, _c, box, _target = _pre_cooled_engine(monkeypatch, from_mode="heat")  # a real heat -> cool change
    assert "pre_cool" in _DELIBERATE_MODE_REASONS
    reasons = [row.get("reason") for row in engine._comfort_mode_changes]
    assert "pre_cool" in reasons, reasons
    box[0] = box[0] + timedelta(minutes=30)
    assert engine.minutes_since_mode_set("cool", box[0], _DELIBERATE_MODE_REASONS) == pytest.approx(30.0, abs=0.5)


def test_away_band_is_never_replaced(monkeypatch):
    from custom_components.climate_advisor.const import OCCUPANCY_AWAY

    engine, c, _box, target = _pre_cooled_engine(monkeypatch)
    engine.set_occupancy_mode(OCCUPANCY_AWAY)
    band = engine._resolved_comfort_band(c)
    assert band.ceiling != target and band.context == "away"


# ---------------------------------------------------------------------------
# The reported day, through the real engine (generated day 105000004, Issue #1371)
# ---------------------------------------------------------------------------


def test_reported_day_has_no_daytime_pre_cool_and_settles():
    """Before the fix: pre-cool at 10:30 AM, heat at 11:00, cool at 12:35, and P10 heat_cool_settles warned.
    Fails if the overnight-only trigger or the awake-hours backstop is removed."""
    import logging

    from tools.sim_harness.generator import build_plan, run_plan

    logging.disable(logging.CRITICAL)
    try:
        rec = run_plan(build_plan(105000004, "coordinator"), probe=True)
    finally:
        logging.disable(logging.NOTSET)
    pre_cools = [ts for typ, _d, ts in rec.event_log if typ == "pre_cool_applied"]
    assert all(ts.hour < 7 or ts.hour >= 22 for ts in pre_cools), pre_cools
    hits = [h.promise for h in (*rec.report.hard, *rec.report.warnings)]
    assert "heat_cool_settles" not in hits, hits


def test_scheduled_before_wake_rolls_to_the_coming_night():
    """At 05:00 (before wake), wake - 4 h today (02:30) is already past; the caller used to skip pre-cool for the
    day. The night that banks for the coming hot day is the next one."""
    c = SimpleNamespace(setback_modifier=-3.0, tomorrow_high=70.0, window_close_time=time(10, 0))
    cfg = {"comfort_cool": 74.0, "sleep_cool": 78.0, "sleep_time": "22:30", "wake_time": "06:30"}
    now = datetime(2026, 7, 1, 5, 0, tzinfo=UTC)
    assert _compute_pre_cool_trigger_time_pure(c, cfg, now) == datetime(2026, 7, 2, 2, 30, tzinfo=UTC)
