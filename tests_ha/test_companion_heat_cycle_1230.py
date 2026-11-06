"""Real-HA A/B run for Issue #1230 R1b: a whole-degree thermostat with a finer companion sensor, heat all day.

Occupant view: the hallway thermostat shows 69 F in whole degrees and decides when its own heat fires from its own
sensor; the same device exposes that sensor at 0.18 F steps. With the companion setting ``auto`` Climate Advisor reads
the finer value; with ``off`` it reads the whole-degree attribute. The person in the home must not feel the switch as
extra mode flips, extra setpoint writes, new household-promise hits or new comfort incidents. The REAL coordinator,
engine and thermostat listener run against the dev thermostat sim (deadband 1.4 F / stop 0.9 F, like the owner's).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_advisor.invariant_watchdog import PROMISES, PromiseConfig

pytestmark = pytest.mark.house

_COMPANION = "sensor.house_thermostat_current_temperature"
_STEP_F = 0.18  # the owner's companion resolution

_HOUSE = {
    "contacts": [],
    "startup": "settled",
    "profile": "closed_loop",
    "start": datetime(2026, 1, 15, 6, 0, tzinfo=UTC),
    "initial_indoor_f": 70.0,
    "outdoor_f": 40.0,
    "high_f": 48.0,
    "low_f": 35.0,
    "initial_hvac_mode": "heat",
    "sim_config": {
        "deadband_heat_f": 1.4,
        "off_offset_heat_f": -0.9,
        "min_run_seconds": 120,
        "min_off_seconds": 120,
    },
}


def _sim(house):
    return house.hass.data["ca_dev_thermostat_sim"][house.sim_entry.entry_id]["thermostat_entity"]


def _mirror(house) -> None:
    value = round(round(_sim(house)._current_temp / _STEP_F) * _STEP_F, 2)
    house.hass.states.async_set(
        _COMPANION, f"{value:.2f}", {"unit_of_measurement": "°F", "device_class": "temperature"}
    )


async def _attach_companion(house) -> None:
    """Put the sim climate entity on a device with a temperature sensor, as the owner's thermostat integration does."""
    hass = house.hass
    host = MockConfigEntry(domain="test")
    host.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=host.entry_id, identifiers={("test", "thermo")})
    ent_reg = er.async_get(hass)
    climate_id = _sim(house).entity_id
    ent_reg.async_update_entity(climate_id, device_id=device.id)
    ent_reg.async_get_or_create(
        "sensor",
        "test",
        "thermo-temp",
        suggested_object_id=_COMPANION.split(".")[1],
        device_id=device.id,
        original_device_class="temperature",
    )
    _mirror(house)


async def _run(house, hours: int) -> dict:
    coord = house.coordinator
    hits: set[str] = set()
    for i in range(hours * 120):  # 30 s slices
        await house.advance(seconds=30)
        _mirror(house)
        if i % 60 == 59:  # every 30 min: every registered promise over the journal's window
            window = coord._promise_journal.window(dt_util.now())
            for spec in PROMISES:
                for hit in spec.check(window, PromiseConfig()):
                    hits.add(f"P{spec.code}:{getattr(hit, 'start', '')}")
    calls = [c for c in house.recorder.select("climate") if c.origin.startswith("ca")]
    modes = [c.data.get("hvac_mode") for c in calls if c.data.get("hvac_mode")]
    incidents = [
        e
        for e in coord._event_log
        if e.get("type") == "incident_detected"
        and e.get("incident_class") in ("comfort_undertemp", "comfort_violation", "comfort_overtemp")
    ]
    model = coord.learning.get_thermal_model()
    return {
        "ca_climate_calls": len(calls),
        "set_temperature": len([c for c in calls if c.service == "set_temperature"]),
        "mode_writes": modes,
        "promise_hits": sorted(hits),
        "incidents": len(incidents),
        "violation_min": coord._today_record.comfort_violations_minutes,
        "swing_heat_f": model.get("swing_heat_f"),
        "companion_in_use": coord._get_indoor_temp_with_provenance().companion_entity,
    }


@pytest.mark.parametrize("setting", ["auto", "off"])
async def test_heat_day_with_a_finer_companion(make_house, setting, record_property) -> None:
    house = await make_house(
        **_HOUSE, ca_config={"thermostat_companion_sensor": setting}, before_ca_setup=_attach_companion
    )
    assert house.coordinator._companion.entities == (_COMPANION,)
    result = await _run(house, 15)
    record_property("result", result)
    print(f"\nCOMPANION_AB setting={setting} {result}")  # noqa: T201 - the A/B report
    assert result["companion_in_use"] == (_COMPANION if setting == "auto" else None)
    assert result["promise_hits"] == [], result
    assert result["incidents"] == 0, result
    # No mode thrash: CA never flips the thermostat out of heat on a cold day.
    assert set(result["mode_writes"]) <= {"heat"}, result
