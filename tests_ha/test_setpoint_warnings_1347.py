"""Issue #1347 against real Home Assistant: setpoint warnings through HA's own options-flow manager.

Occupant view: on Settings -> Climate Advisor -> Configure -> Temperature Setpoints, a combination that
would make the heat and AC take turns shows a warning next to the field and keeps what was typed;
pressing Submit again saves it. A mild away heat (20 °C, the reporter's mini-split case) saves at once.
A separate vacation heat reaches the thermostat's band once the zone reloads.
"""

from __future__ import annotations

import pytest
from homeassistant.data_entry_flow import FlowResultType


async def _open_setpoints(hass, entry_id: str) -> dict:
    result = await hass.config_entries.options.async_init(entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "setpoints"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "setpoints"
    return result


async def test_inverted_band_warns_then_identical_resubmit_saves(make_house) -> None:
    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    hass, entry = house.hass, house.ca_entry
    form = await _open_setpoints(hass, entry.entry_id)
    values = {
        "comfort_heat": 74.0,
        "comfort_cool": 72.0,  # heat above cool: the heat and AC would fight
        "setback_heat": 60.0,
        "setback_cool": 80.0,
        "sleep_heat": 66.0,
        "sleep_cool": 78.0,
    }

    first = await hass.config_entries.options.async_configure(form["flow_id"], dict(values))
    assert first["type"] is FlowResultType.FORM
    assert first["errors"] == {"comfort_heat": "setpoint_band_inverted_warning"}
    assert entry.data["comfort_heat"] != 74.0  # nothing saved yet

    second = await hass.config_entries.options.async_configure(first["flow_id"], dict(values))
    assert second["type"] is FlowResultType.MENU  # back to the options menu: saved
    assert (entry.data["comfort_heat"], entry.data["comfort_cool"]) == (74.0, 72.0)


async def test_mild_celsius_away_heat_saves_at_once(make_house) -> None:
    """The #1347 reporter: away heat 20 °C was refused by the old 18.5 °C cap."""
    house = await make_house(units="metric", profile="scripted", initial_indoor_f=72.0)
    hass, entry = house.hass, house.ca_entry
    hass.config_entries.async_update_entry(entry, data={**entry.data, "temp_unit": "celsius"})
    form = await _open_setpoints(hass, entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            "comfort_heat": 21.0,
            "comfort_cool": 25.0,
            "setback_heat": 20.0,
            "setback_cool": 28.0,
            "sleep_heat": 20.0,
            "sleep_cool": 26.0,
        },
    )
    assert result["type"] is FlowResultType.MENU, result.get("errors")
    assert entry.data["setback_heat"] == pytest.approx(68.0)  # 20 °C stored as °F


async def test_separate_vacation_heat_reaches_the_band(make_house) -> None:
    house = await make_house(
        units="us",
        profile="scripted",
        initial_indoor_f=72.0,
        ca_config={"vacation_toggle_entity": "input_boolean.vacation"},
        extra_states={"input_boolean.vacation": "on"},
    )
    hass, entry = house.hass, house.ca_entry
    form = await _open_setpoints(hass, entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            "comfort_heat": 70.0,
            "comfort_cool": 75.0,
            "setback_heat": 60.0,
            "setback_cool": 80.0,
            "sleep_heat": 66.0,
            "sleep_cool": 78.0,
            "vacation_heat": 52.0,
            "vacation_cool": 88.0,
        },
    )
    assert result["type"] is FlowResultType.MENU, result.get("errors")
    await house.reload_zone()
    await house.advance(minutes=40)
    heat, cool = house.sensor("heat_threshold"), house.sensor("cool_threshold")
    assert heat.attributes["context"] == "vacation"
    assert (float(heat.state), float(cool.state)) == pytest.approx((52.0, 88.0))
