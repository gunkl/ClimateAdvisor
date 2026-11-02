"""Real-HA test for Issue #1211 (O5): the daily record's "windows were recommended today" flag.

Occupant story: the forecast says a warm day with a cool morning (windows day), but the house starts cold with colder air
outside, so the live gate vetoes the recommendation (opening windows would chill the house further). The
window-compliance statistics must not count that day as a day the occupant was asked to open windows: the
record's flag (the denominator) has to read the same gated flag the credit uses, not the raw forecast answer.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_HOUSE = {
    "fan": None,
    "contacts": ["window"],
    "startup": "settled",
    "start": datetime(2026, 7, 15, 7, 0, tzinfo=UTC),
    "initial_indoor_f": 66.0,
    "high_f": 82.0,
    "low_f": 55.0,
    "initial_hvac_mode": "off",
}


async def test_a_day_the_live_gate_vetoes_all_day_is_not_counted_as_a_windows_day(make_house) -> None:
    house = await make_house(**{**_HOUSE, "outdoor_f": 58.0})  # cold house, colder outdoor: windows would chill it
    await house.advance(minutes=40)  # more than one 30-minute classification cycle
    c = house.coordinator.automation_engine._current_classification
    assert c.forecast_windows_recommended is True, "precondition: the forecast alone says windows day"
    assert c.windows_recommended is False, "precondition: the live gate vetoed it"
    record = house.coordinator._today_record
    assert record is not None
    assert record.windows_recommended is False, "the record must not count a gate-vetoed day as a windows day"


async def test_positive_control_a_day_the_gate_allows_is_counted(make_house) -> None:
    house = await make_house(**{**_HOUSE, "outdoor_f": 62.0, "initial_indoor_f": 78.0})
    await house.advance(minutes=40)
    assert house.coordinator.automation_engine._current_classification.windows_recommended is True
    assert house.coordinator._today_record.windows_recommended is True
