"""Issue #1425: a short Away is charted as one flat Away period, not band spikes (and never a setback while home).

Occupant story (production, 2026-11-07): the windows were open, so Climate Advisor held the HVAC off and skipped the
away setback; nothing was sent to the thermostat. Coming home with the windows still open deferred the comfort restore
(Issue #1050), and the chart read that as "the thermostat is still on the away setback", drawing 63-79 F at 12:23
while you were home. The deferral itself is unchanged (the restore still waits for the window); the "still held"
claim now needs a setback that was actually sent in this away period.

The chart side: the past band is held flat from one row to the next (no slope into a single Away row), and the Away
strip runs from the row you left on to the row you came home on. The real-HA timeline is
``tests_ha/test_away_band_chart_1425.py``.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from tests.test_occupancy_restore_hold import _engine, _home, _names

_FE = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "frontend"
_CHART = (_FE / "chart_v2.js").read_text(encoding="utf-8")


def _skipped_setback_engine():
    eng, events, call = _engine()
    eng._away_setback_sent = False  # the setback was skipped: a door/window pause held the HVAC off
    return eng, events, call


class TestRestoreHeldOnlyAfterASentSetback:
    def test_window_open_after_a_skipped_setback_is_not_a_setback_hold(self):
        eng, events, call = _skipped_setback_engine()
        eng._paused_by_door = True
        _home(eng)
        # The restore still waits for the window (behaviour unchanged) ...
        assert call.await_count == 0
        assert [d["reason"] for e, d in events if e == "occupancy_restore_deferred"] == ["paused_by_door"]
        assert "occupancy_comfort_restored" not in _names(events)
        # ... but nothing was set back, so the chart/Status must not be told the thermostat is on the setback.
        assert eng.occupancy_restore_hold_reason() is None, "#1425: a setback that was never sent drawn as held"

    def test_leaving_with_the_window_open_sends_nothing_and_marks_nothing(self):
        eng, _events, call = _engine()
        eng.set_occupancy_mode("home")
        eng.set_occupancy_mode("away")  # a new away period
        eng._paused_by_door = True
        asyncio.run(eng.handle_occupancy_away())
        assert call.await_count == 0, "precondition: the open window skips the setback"
        assert eng._away_setback_sent is False

    def test_leaving_with_the_windows_closed_marks_the_setback_sent_and_a_later_hold_draws_it(self):
        eng, _events, call = _engine()
        eng.set_occupancy_mode("home")
        eng.set_occupancy_mode("away")
        assert eng._away_setback_sent is False, "a new away period starts with nothing sent"
        asyncio.run(eng.handle_occupancy_away())
        assert call.await_count >= 1, "precondition: the setback was sent"
        assert eng._away_setback_sent is True
        eng._paused_by_door = True  # you come home and open a window
        _home(eng)
        assert eng.occupancy_restore_hold_reason() == "paused_by_door", "a sent setback is still held (#1050)"

    def test_away_to_vacation_keeps_the_away_period(self):
        eng, _events, _call = _engine()  # setback sent while away
        eng.set_occupancy_mode("vacation")
        assert eng._away_setback_sent is True

    def test_a_restore_ends_the_away_period(self):
        eng, events, _call = _engine()
        _home(eng)
        assert "occupancy_comfort_restored" in _names(events)
        assert eng._away_setback_sent is False


class TestChartDrawsTheAwayPeriodFlat:
    def test_the_past_band_is_held_flat_until_the_next_row(self):
        step = re.search(r"  function _stepHeld\(pts\) \{.*?\n  \}\n", _CHART, re.DOTALL)
        assert step, "_stepHeld not found"
        # a change is drawn at the next row's time, from the previous row's value
        assert "if (prev && prev.y !== p.y) out.push({ x: p.x, y: prev.y });" in step.group(0)
        # both past band edges go through it; the forecast band (its wake/sleep ramps) does not
        assert "[..._stepHeld(histBandUpper), ...futBandUpper]" in _CHART
        assert "[..._stepHeld(histBandLower), ...futBandLower]" in _CHART

    def test_the_away_strip_runs_from_its_row_to_the_next(self):
        # Issue #1436: every past strip now holds from its row to the next row (never past NOW), Away included
        assert "push(ts, Math.min(next, now), false);" in _CHART
