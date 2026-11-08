"""Real-HA test for Issue #1404: during a time-of-use bank the status payload and the briefing quote the bank.

Occupant story: a hot afternoon with a 3:00 PM high-cost period. Climate Advisor pre-cools ahead of it: the
thermostat is told cool 68 F (the comfort floor), while the person's band with Prefer savings on is 66-78 F. Before
the fix the Status sentence, the status API's ``ca_target_heat`` / ``ca_target_cool`` (the reading card's
"Climate Advisor wants ..."), the Cool Threshold sensor and the briefing's HVAC Mode row all quoted the schedule band
(cool 78 F), so the person could not tell what the house was held to or when it went back.

Real Home Assistant, the real coordinator (its own 5-minute bank check opens the bank), the real status view and the
real briefing builder. Explicit asserts only (no snapshot entry is added).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor.api import ClimateAdvisorStatusView

pytestmark = pytest.mark.house

_PEAK = {"id": "peak1", "name": "Afternoon Peak", "days": ["all"], "start": "15:00", "end": "19:00", "cost_tag": "high"}
_HOT = {
    "start": datetime(2026, 7, 15, 13, 50, tzinfo=UTC),
    "initial_indoor_f": 75.0,
    "outdoor_f": 92.0,
    "high_f": 96.0,
    "low_f": 72.0,
    "initial_hvac_mode": "cool",
}
_CONFIG = {
    "comfort_heat": 68.0,
    "comfort_cool": 76.0,
    "setback_heat": 60.0,
    "setback_cool": 80.0,
    "aggressive_savings": True,  # awake band 66-78 F: the bank target 68 F is NOT a band edge
    "schedules": [_PEAK],
    "default_tou_lead_minutes": 45,
}


async def _status(hass, entry_id: str) -> dict:
    request = SimpleNamespace(
        app={"hass": hass}, query={"entry_id": entry_id}, method="GET", path="/api/climate_advisor/status", remote="t"
    )
    response = await ClimateAdvisorStatusView().get(request)
    return json.loads(response.text)


async def _banked_house(make_house, units: str = "us"):
    house = await make_house(**_HOT, units=units, ca_config=dict(_CONFIG))
    # The bank window opens 45 minutes before the 15:00 period (no learned rate yet: the default lead), at 14:15;
    # the 5-minute bank check tells the thermostat the bank target within five minutes of that.
    await house.advance_to(datetime(2026, 7, 15, 14, 25, tzinfo=UTC))
    coord = house.coordinator
    assert coord._current_classification is not None and coord._current_classification.hvac_mode == "cool"
    return house, coord


async def test_status_payload_and_briefing_quote_the_bank(make_house):
    house, coord = await _banked_house(make_house)
    assert house.thermostat.mode == "cool"
    assert house.thermostat.setpoint_f == pytest.approx(68.0, abs=0.5), "precondition: the bank is on the thermostat"

    body = await _status(house.hass, house.ca_entry.entry_id)
    sentence = "Pre-cooling to 68°F until the 3:00 PM high-cost period, then back to the comfort band 66–78°F"
    # the thresholds the reading card compares the setpoint with: no "Climate Advisor wants 78 F" any more
    assert (body["ca_target_heat"], body["ca_target_cool"]) == (66.0, 68.0)
    hold = body["current_hold"]
    assert hold is not None and hold["kind"] == "tou_bank" and hold["mode"] == "cool"
    assert hold["target"] == 68.0 and hold["text"] == sentence
    assert (hold["schedule_heat"], hold["schedule_cool"]) == (66.0, 78.0)
    assert hold["ends"].startswith("2026-07-15T15:00")
    # the Status card says the same, refreshed by the bank check (not 30 minutes later)
    assert body["automation_status"] == sentence[0].lower() + sentence[1:]
    assert house.sensor("cool_threshold").state == "68.0"

    _full, short = coord._build_briefing_text(coord._current_classification, suggestions=[])
    row = next(ln.strip() for ln in short.splitlines() if ln.strip().startswith("HVAC Mode:"))
    assert row == "HVAC Mode: Cool at 68°F until the 3:00 PM high-cost period, then back to the comfort band 66–78°F"
    full, _ = coord._build_briefing_text(coord._current_classification, suggestions=[])
    assert f"Now: {sentence}." in full

    assert coord.timeline_payload()["current_hold"]["text"] == sentence
    action, at = coord._compute_next_automation_action(coord._current_classification)
    assert (action, at) == ("High-cost period starts — back to the comfort band 66–78°F", "3:00 PM")


async def test_after_the_bank_the_schedule_band_returns(make_house):
    """Control: once the high-cost period starts the bank is over and every surface is back on the schedule band."""
    house, coord = await _banked_house(make_house)
    await house.advance_to(datetime(2026, 7, 15, 15, 31, tzinfo=UTC))
    body = await _status(house.hass, house.ca_entry.entry_id)
    assert body["current_hold"] is None
    assert (body["ca_target_heat"], body["ca_target_cool"]) == (66.0, 78.0)
    assert not body["automation_status"].startswith("pre-cooling")


async def test_metric_install_quotes_the_bank_in_celsius(make_house):
    house, _coord = await _banked_house(make_house, units="metric")
    body = await _status(house.hass, house.ca_entry.entry_id)
    hold = body["current_hold"]
    assert hold is not None and hold["target"] == pytest.approx(20.0, abs=0.1)
    assert body["ca_target_cool"] == pytest.approx(20.0, abs=0.1)
    assert hold["text"].startswith("Pre-cooling to 20°C until the 3:00 PM high-cost period")
