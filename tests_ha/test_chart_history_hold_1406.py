"""Real-HA test for Issue #1406: the chart history draws a past time-of-use bank as what the thermostat held.

Occupant story: a hot afternoon with a 3:00 PM high-cost period and Prefer savings on (band 66-78 F). Climate Advisor
pre-cools ahead of it: the thermostat is told cool 68 F, and the live chart (since #1404) draws the hold, 66-68 F.
Before the fix, scrolling back over that afternoon drew the band schedule's shape instead (68-78 F), because chart_log
stores only the band schedule (``lower``/``upper``, which the replay tools, the prediction and the comfort score read).

Real Home Assistant, the real coordinator's own chart_log rows (its 30-minute poll and event rows), and the real REST
``chart_data`` view for a historical viewport. Explicit asserts only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor.api import ClimateAdvisorChartDataView

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
    "aggressive_savings": True,  # awake band 66-78 F; the bank target 68 F is not a band edge
    "schedules": [_PEAK],
    "default_tou_lead_minutes": 45,  # the bank opens at 14:15
}
_BANK_START = datetime(2026, 7, 15, 14, 20, tzinfo=UTC)
_BANK_END = datetime(2026, 7, 15, 15, 0, tzinfo=UTC)


async def _chart_data(hass, entry_id: str, *, before: datetime, range_str: str = "6h") -> dict:
    query = {"entry_id": entry_id, "range": range_str, "before_ts": str(int(before.timestamp() * 1000))}
    request = SimpleNamespace(
        app={"hass": hass},
        query=query,
        rel_url=SimpleNamespace(query=query),
        method="GET",
        path="/api/climate_advisor/chart_data",
        remote="t",
    )
    response = await ClimateAdvisorChartDataView().get(request)
    return json.loads(response.text)


def _rows(coord, start: datetime, end: datetime) -> list[dict]:
    out = []
    for e in coord._chart_log.raw_entries():
        ts = datetime.fromisoformat(e["ts"])
        if start <= ts < end:
            out.append(e)
    return out


@pytest.mark.parametrize("units", ["us", "metric"])
async def test_a_past_bank_is_drawn_as_the_hold(make_house, units):
    house = await make_house(**_HOT, units=units, ca_config=dict(_CONFIG))
    await house.advance_to(datetime(2026, 7, 15, 14, 55, tzinfo=UTC))
    coord = house.coordinator
    assert house.thermostat.setpoint_f == pytest.approx(68.0, abs=0.5), "precondition: the bank is on the thermostat"
    await house.advance_to(datetime(2026, 7, 15, 17, 30, tzinfo=UTC))  # > 1 h after the viewport: a historical view

    before = [e for e in _rows(coord, _HOT["start"], datetime(2026, 7, 15, 14, 15, tzinfo=UTC)) if "lower" in e]
    banked = [e for e in _rows(coord, _BANK_START, _BANK_END) if e.get("lower") is not None]
    after = [
        e for e in _rows(coord, datetime(2026, 7, 15, 15, 5, tzinfo=UTC), datetime(2026, 7, 15, 16, 5, tzinfo=UTC))
    ]
    assert banked, "the coordinator wrote chart_log rows during the bank"
    for e in banked:
        # lower/upper keep their meaning: the band schedule's bucket in force (the 14:00 hour, which starts before the
        # 14:15 window opens, so it is the 66-78 band; the live chart draws the hour in force as the live hold)
        assert (e["lower"], e["upper"]) == pytest.approx((66.0, 78.0), abs=0.05)
        # the hold the live chart drew: cool to 68 inside the 66-78 band
        assert (e["hold_lower"], e["hold_upper"]) == pytest.approx((66.0, 68.0), abs=0.05)
    for e in before + after:  # outside the bank the chart draws the band schedule: no new keys on the row
        assert "hold_lower" not in e and "hold_upper" not in e, e

    body = await _chart_data(house.hass, house.ca_entry.entry_id, before=datetime(2026, 7, 15, 16, 0, tzinfo=UTC))
    conv = (lambda f: f) if units == "us" else (lambda f: round((f - 32) * 5 / 9, 1))
    bank_ts = {e["ts"] for e in banked}
    drawn = [b for b in body["target_band"] if b["ts"] in bank_ts]
    assert drawn and all(
        (b["lower"], b["upper"]) == pytest.approx((conv(66.0), conv(68.0)), abs=0.06) for b in drawn
    ), drawn
    logged = [e for e in body["state_log"] if e["ts"] in bank_ts]
    assert logged and all(
        (e["hold_lower"], e["hold_upper"]) == pytest.approx((conv(66.0), conv(68.0)), abs=0.06) for e in logged
    )


# ── Issue #1459: chart_data carries what the zone has set up ─────────────────────────────────────────────────────────
async def _live_chart_data(hass, entry_id: str) -> dict:
    query = {"entry_id": entry_id, "range": "24h"}
    request = SimpleNamespace(
        app={"hass": hass},
        query=query,
        rel_url=SimpleNamespace(query=query),
        method="GET",
        path="/api/climate_advisor/chart_data",
        remote="t",
    )
    response = await ClimateAdvisorChartDataView().get(request)
    return json.loads(response.text)


_CAPS_KEYS = {
    "v4_running",
    "outdoor_sensor",
    "fan",
    "door_sensors",
    "occupancy",
    "humidity_indoor",
    "humidity_outdoor",
}


@pytest.mark.parametrize("set_up", [True, False])
async def test_chart_data_carries_the_zone_capabilities(make_house, set_up):
    """Occupant view: a house with a whole-house fan, a patio door sensor, a Home switch, a patio thermometer and
    humidity sensors gets every chart-key control switchable; a bare house (v3 engine, nothing else) gets them locked.
    Through the real REST view, the real coordinator, real Home Assistant."""
    from tests_ha.house.builder import OUTDOOR_F_SENSOR

    if set_up:
        house = await make_house(
            fan="whf",
            contacts=["patio_door"],
            extra_states={"input_boolean.home": "on", "sensor.room_rh": "48", "sensor.patio_rh": "62"},
            ca_config={
                "outdoor_temp_source": "sensor",
                "outdoor_temp_entity": OUTDOOR_F_SENSOR,
                "home_toggle_entity": "input_boolean.home",
                "indoor_humidity_entity": "sensor.room_rh",
                "outdoor_humidity_entity": "sensor.patio_rh",
            },
        )
    else:
        house = await make_house(ca_config={"primary_engine": "v3"})
    body = await _live_chart_data(house.hass, house.ca_entry.entry_id)
    caps = body["capabilities"]
    assert set(caps) == _CAPS_KEYS
    if set_up:
        assert caps == dict.fromkeys(_CAPS_KEYS, True), caps
    else:
        # the sim thermostat reports no current_humidity and the house weather no humidity: no humidity source
        assert caps == dict.fromkeys(_CAPS_KEYS, False), caps
