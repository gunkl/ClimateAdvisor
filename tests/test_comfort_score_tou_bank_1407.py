"""Issue #1407 (decision C22, reversible): the Comfort Score during a time-of-use bank.

Occupant view: while the house pre-cools or pre-heats for a high-cost period (a "bank") the thermostat is told the
bank target, but the Comfort Score read the cached chart band, which draws a bank in the #786 shape (target on the
opposite edge). An Away pre-heat to 63 F read 60-63 F, so a house at 70 F, well inside the Away band, counted as
uncomfortable. During a bank a minute now counts only when the house is outside BOTH the hold (what the thermostat
is told, ``tou_bank_band()``) and the schedule band (home / sleep / Away / Vacation): a pre-cool pulling the house
from 74 F toward 68 F is inside the 68-76 F comfort range, so the person is comfortable and those minutes must not
feed the ``comfort_violations`` suggestion. The score reads the CONTROL source ``_tou_bank_in_force()``, never the
display resolver. Score only: no thermostat write.
"""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import custom_components.climate_advisor.coordinator as cm
from custom_components.climate_advisor.const import OCCUPANCY_HOME
from custom_components.climate_advisor.scheduler import TOUPhase
from tests.test_current_hold_1404 import NOW, OPEN, PEAK, _coordinator


@pytest.fixture(autouse=True)
def _frozen_now(monkeypatch):
    monkeypatch.setattr(cm.dt_util, "now", lambda time_zone=None: NOW)


def _scored(chart: tuple[float, float] = (77.0, 80.0), **kw):
    """A bank coordinator whose cached chart band at NOW is ``chart`` (the #786 shape for the default Away pre-cool)."""
    coord = _coordinator(**kw)
    coord._target_band_schedule = [
        {"ts": (NOW - timedelta(minutes=30)).isoformat(), "lower": chart[0], "upper": chart[1]}
    ]
    return coord


class TestBankBands:
    def test_away_pre_cool_is_the_hold_and_the_away_band(self) -> None:
        assert _scored()._comfort_score_bank_bands(NOW) == ((60.0, 77.0), (60.0, 80.0))

    def test_away_pre_heat(self) -> None:
        assert _scored(mode="heat", target=63.0)._comfort_score_bank_bands(NOW) == ((63.0, 80.0), (60.0, 80.0))

    def test_home_pre_cool_to_the_floor(self) -> None:
        bands = _scored(occupancy=OCCUPANCY_HOME, target=68.0)._comfort_score_bank_bands(NOW)
        assert bands == ((68.0, 68.0), (68.0, 76.0))

    def test_no_bank_outside_its_window_or_phase(self) -> None:
        assert _scored(phase=TOUPhase.NONE)._comfort_score_bank_bands(NOW) is None
        coord = _scored()
        assert coord._comfort_score_bank_bands(PEAK) is None  # the bank ended at the period start
        assert coord._comfort_score_bank_bands(OPEN - timedelta(seconds=1)) is None  # not opened yet

    def test_a_deferred_bank_is_not_a_bank(self) -> None:
        coord = _scored()
        coord.automation_engine._manual_override_active = True  # your change holds the thermostat, not the bank
        assert coord._comfort_score_bank_bands(NOW) is None

    def test_a_fault_falls_back(self) -> None:
        coord = _scored()
        coord._tou_bank_in_force = MagicMock(side_effect=RuntimeError("boom"))
        assert coord._comfort_score_bank_bands(NOW) is None

    def test_celsius_install_scores_in_internal_fahrenheit(self) -> None:
        """Indoor is internal °F on every install, so the bands are too (a 25 °C target = 77 °F)."""
        assert _scored(unit="celsius")._comfort_score_bank_bands(NOW) == ((60.0, 77.0), (60.0, 80.0))

    def test_the_hold_equals_what_every_surface_quotes(self) -> None:
        """Control side and display side agree on the hold (the display resolver reads the same control source)."""
        coord = _scored()
        hold = coord.current_hold(NOW)
        assert coord._comfort_score_bank_bands(NOW)[0] == (hold.band.floor, hold.band.ceiling)


class TestMinutes:
    """End of the path: ``_track_indoor_comfort`` adds the minutes, and writes nothing to the thermostat."""

    def _minutes(self, coord, indoor: float) -> float:
        coord._indoor_temp_history = []
        coord._today_record = MagicMock(comfort_violations_minutes=0.0)
        coord._last_violation_check = None  # first check of the day counts a full 30 min
        coord.hass = coord.automation_engine.hass
        coord._track_indoor_comfort(indoor)
        coord.automation_engine.hass.services.async_call.assert_not_called()
        return coord._today_record.comfort_violations_minutes

    def test_pull_down_inside_the_comfort_range_is_not_a_violation(self) -> None:
        """Home pre-cool to the 68 F floor, house still at 74 F: outside the hold, inside 68-76 F, so comfortable."""
        assert self._minutes(_scored(chart=(68.0, 76.0), occupancy=OCCUPANCY_HOME, target=68.0), 74.0) == 0.0

    def test_outside_both_is_still_counted(self) -> None:
        """78 F in that pre-cool: above the hold AND 2 F past the 76 F ceiling (beyond the 1.5 F swing)."""
        assert self._minutes(_scored(chart=(68.0, 76.0), occupancy=OCCUPANCY_HOME, target=68.0), 78.0) == 30.0

    def test_away_pre_cool_above_the_away_ceiling_counts(self) -> None:
        assert self._minutes(_scored(), 82.0) == 30.0

    def test_within_the_swing_of_the_schedule_edge_does_not_count(self) -> None:
        assert self._minutes(_scored(), 81.0) == 0.0

    def test_away_pre_heat_above_its_target_is_not_a_violation(self) -> None:
        """70 F in an Away pre-heat to 63 F: inside the Away band 60-80 F."""
        coord = _scored(chart=(60.0, 63.0), mode="heat", target=63.0)
        assert self._minutes(coord, 70.0) == 0.0

    def test_negative_control_the_786_band_counted_it(self) -> None:
        """Effectiveness: the same reading on the cached #786 band (no bank owning the thermostat) counts 30 min."""
        coord = _scored(chart=(60.0, 63.0), mode="heat", target=63.0, phase=TOUPhase.NONE)
        assert self._minutes(coord, 70.0) == 30.0


def test_the_bank_bands_feed_only_the_score() -> None:
    """The bank may reach the score but never a decision: ``_comfort_score_bank_bands`` has exactly one caller, the
    Comfort Score minutes (``_track_indoor_comfort``), in the whole package."""
    pkg = Path(cm.__file__).resolve().parent
    callers = []
    for path in sorted(pkg.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    if isinstance(node, ast.Attribute) and node.attr == "_comfort_score_bank_bands":
                        callers.append(f"{path.name}:{fn.name}")
    assert callers == ["coordinator.py:_track_indoor_comfort"]
