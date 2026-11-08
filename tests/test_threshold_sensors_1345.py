"""Issue #1345: Heat Threshold / Cool Threshold sensors.

Occupant story: someone runs Climate Advisor in observe-only mode beside their own controller and wants to graph
the band Climate Advisor would hold the house to. The two band edges must be sensors (HA keeps history for states,
not attributes), they must follow the band actually in force (comfort, Prefer Savings, sleep, away, vacation, a
time-of-use widening) and they must equal the status API's ``ca_target_heat`` / ``ca_target_cool`` -- if they ever
drifted apart, the graph would show a band the thermostat is not armed with.

Every test drives the real sensor classes and the real status view (Issue #452 stub pattern).
"""

from __future__ import annotations

import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import pytest  # noqa: E402

from custom_components.climate_advisor.automation import (  # noqa: E402
    BAND_CONTEXTS,
    active_thresholds_for,
    select_comfort_band,
)
from custom_components.climate_advisor.const import (  # noqa: E402
    CEILING_ESCALATION_SAVINGS_MARGIN_F,
    VACATION_SETBACK_EXTRA,
)
from custom_components.climate_advisor.engine_inputs import TouAvWindow  # noqa: E402
from custom_components.climate_advisor.sensor import (  # noqa: E402
    ClimateAdvisorCoolThresholdSensor,
    ClimateAdvisorHeatThresholdSensor,
)
from custom_components.climate_advisor.temperature import display_temp  # noqa: E402
from tests.test_api import _make_classification, _status_get_with_climate_state  # noqa: E402

DAY = datetime(2026, 7, 21, 14, 0, 0)  # outside the 22:30-07:00 sleep window
NIGHT = datetime(2026, 7, 21, 2, 0, 0)  # inside it

_CONFIG = {
    "comfort_heat": 68.0,
    "comfort_cool": 74.0,
    "sleep_heat": 64.0,
    "sleep_cool": 72.0,
    "setback_heat": 60.0,
    "setback_cool": 80.0,
    "sleep_time": "22:30",
    "wake_time": "07:00",
}


def _coordinator(
    config: dict | None = None,
    *,
    occupancy_mode: str = "home",
    classification=None,
    no_classification: bool = False,
    tou_expansion_f: float = 0.0,
):
    coord = MagicMock()
    coord.config = dict(config or _CONFIG)
    coord.data = {}
    coord._occupancy_mode = occupancy_mode
    coord.current_classification = (
        None if no_classification else (classification if classification is not None else _make_classification())
    )
    ae = MagicMock()
    ae._active_tou_av_window.return_value = TouAvWindow(expansion_f=tou_expansion_f)
    coord.automation_engine = ae
    return coord


def _entry():
    entry = MagicMock()
    entry.entry_id = "zone1"
    entry.title = "Downstairs"
    return entry


def _read(coord, now):
    """(heat, cool, heat attrs, cool attrs) from the real sensors at ``now``."""
    from homeassistant.util import dt as dt_util

    heat = ClimateAdvisorHeatThresholdSensor(coord, _entry())
    cool = ClimateAdvisorCoolThresholdSensor(coord, _entry())
    with patch.object(dt_util, "now", return_value=now):
        return heat.native_value, cool.native_value, heat.extra_state_attributes, cool.extra_state_attributes


def _api(coord, now, unit: str = "fahrenheit"):
    """(ca_target_heat, ca_target_cool) from the real status view for the same state."""
    from homeassistant.util import dt as dt_util

    state = MagicMock()
    state.state = "cool"
    state.attributes = {"temperature": 71, "target_temp_low": None, "target_temp_high": None}
    cfg = {**coord.config, "temp_unit": unit}
    kwargs = {
        "occupancy_mode": coord._occupancy_mode,
        "tou_expansion_f": coord.automation_engine._active_tou_av_window().expansion_f,
    }
    if coord.current_classification is None:
        kwargs["no_classification"] = True
    else:
        kwargs["classification"] = coord.current_classification
    with patch.object(dt_util, "now", return_value=now):
        body = _status_get_with_climate_state(cfg, state, **kwargs)
    return body["ca_target_heat"], body["ca_target_cool"]


# (id, coordinator kwargs, config overlay, now, expected heat, expected cool, expected context, provisional)
_CASES = [
    ("comfort", {}, {}, DAY, 68.0, 74.0, "comfort", False),
    (
        "prefer_savings",
        {},
        {"aggressive_savings": True},
        DAY,
        68.0 - CEILING_ESCALATION_SAVINGS_MARGIN_F,
        74.0 + CEILING_ESCALATION_SAVINGS_MARGIN_F,
        "prefer_savings",
        False,
    ),
    ("sleep", {}, {}, NIGHT, 64.0, 72.0, "sleep", False),
    ("sleep_ignores_savings", {}, {"aggressive_savings": True}, NIGHT, 64.0, 72.0, "sleep", False),
    ("away", {"occupancy_mode": "away"}, {}, DAY, 60.0, 80.0, "away", False),
    (
        "vacation",
        {"occupancy_mode": "vacation"},
        {},
        DAY,
        60.0 - VACATION_SETBACK_EXTRA,
        80.0 + VACATION_SETBACK_EXTRA,
        "vacation",
        False,
    ),
    (
        "tou_away_cool_day",
        {"occupancy_mode": "away", "tou_expansion_f": 3.0, "classification": _make_classification(hvac_mode="cool")},
        {},
        DAY,
        60.0,
        83.0,
        "tou_expansion",
        False,
    ),
    (
        "tou_vacation_heat_day",
        {
            "occupancy_mode": "vacation",
            "tou_expansion_f": 3.0,
            "classification": _make_classification(hvac_mode="heat"),
        },
        {},
        DAY,
        60.0 - VACATION_SETBACK_EXTRA - 3.0,
        80.0 + VACATION_SETBACK_EXTRA,
        "tou_expansion",
        False,
    ),
    # A TOU window while home is a no-op for the band (select_comfort_band ignores it): still "comfort".
    ("tou_home_is_noop", {"tou_expansion_f": 3.0}, {}, DAY, 68.0, 74.0, "comfort", False),
    ("restart_day", {"no_classification": True}, {}, DAY, 68.0, 74.0, "comfort", True),
    ("restart_night", {"no_classification": True}, {}, NIGHT, 64.0, 72.0, "sleep", True),
]


@pytest.mark.parametrize(
    ("kwargs", "overlay", "now", "heat_f", "cool_f", "context", "provisional"),
    [c[1:] for c in _CASES],
    ids=[c[0] for c in _CASES],
)
class TestThresholdContexts:
    def test_sensor_values_and_context(self, kwargs, overlay, now, heat_f, cool_f, context, provisional):
        coord = _coordinator({**_CONFIG, **overlay}, **kwargs)
        heat, cool, heat_attrs, cool_attrs = _read(coord, now)
        assert heat == pytest.approx(heat_f) and cool == pytest.approx(cool_f)
        for attrs in (heat_attrs, cool_attrs):
            assert attrs == {"context": context, "provisional": provisional}

    @pytest.mark.parametrize("unit", ["fahrenheit", "celsius"])
    def test_sensors_equal_status_api(self, kwargs, overlay, now, heat_f, cool_f, context, provisional, unit):
        """Parity (#402/#1080 sibling-drift guard): sensor °F, shown in the user's unit, == the API field."""
        coord = _coordinator({**_CONFIG, **overlay}, **kwargs)
        heat, cool, _, _ = _read(coord, now)
        api_heat, api_cool = _api(coord, now, unit)
        assert display_temp(heat, unit) == pytest.approx(api_heat)
        assert display_temp(cool, unit) == pytest.approx(api_cool)


class TestThresholdSensorShape:
    def test_entity_metadata(self):
        from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
        from homeassistant.const import UnitOfTemperature
        from homeassistant.helpers.entity import EntityCategory

        coord = _coordinator()
        for cls, key, name in (
            (ClimateAdvisorHeatThresholdSensor, "heat_threshold", "Heat Threshold"),
            (ClimateAdvisorCoolThresholdSensor, "cool_threshold", "Cool Threshold"),
        ):
            s = cls(coord, _entry())
            assert s._attr_unique_id == f"zone1_{key}"
            assert s._attr_name == name and s._attr_has_entity_name
            assert s._attr_native_unit_of_measurement == UnitOfTemperature.FAHRENHEIT
            assert s._attr_device_class == SensorDeviceClass.TEMPERATURE
            assert s._attr_state_class == SensorStateClass.MEASUREMENT
            assert s._attr_entity_category == EntityCategory.DIAGNOSTIC
            assert getattr(s, "_attr_entity_registry_enabled_default", True) is True

    def test_celsius_zone_keeps_native_fahrenheit(self):
        """A Celsius zone still reports °F natively (config is internal °F); HA converts for display."""
        coord = _coordinator({**_CONFIG, "temp_unit": "celsius"})
        heat, cool, _, _ = _read(coord, DAY)
        assert (heat, cool) == (68.0, 74.0)

    def test_never_the_thermostat_setpoint(self):
        """Unlike Target Temperature, the thresholds ignore the thermostat's live setpoint (observe-only)."""
        coord = _coordinator()
        coord.data = {"effective_target_temp": 71.0, "current_setpoint": 71.0}
        heat, cool, _, _ = _read(coord, DAY)
        assert (heat, cool) == (68.0, 74.0)

    def test_resolver_fault_reads_unavailable_not_crash(self, caplog):
        coord = _coordinator()
        coord.config = None  # resolver cannot run
        heat, cool, heat_attrs, _ = _read(coord, DAY)
        assert heat is None and cool is None
        assert heat_attrs == {"context": None, "provisional": None}
        assert "Threshold unavailable" in caplog.text

    def test_registered_in_async_setup_entry(self):
        import asyncio

        from custom_components.climate_advisor import sensor as sensor_mod

        hass = MagicMock()
        hass.data = {"climate_advisor": {"zone1": _coordinator()}}
        added: list = []
        asyncio.run(sensor_mod.async_setup_entry(hass, _entry(), added.extend))
        names = [type(e).__name__ for e in added]
        assert names.count("ClimateAdvisorHeatThresholdSensor") == 1
        assert names.count("ClimateAdvisorCoolThresholdSensor") == 1
        assert len(added) == 21  # README "Entities": 21 sensors (+1 switch)


class TestSymmetry:
    """Both edges come from ONE band (one resolver call), never two independent paths."""

    def test_single_resolver_call_feeds_both_edges(self):
        from custom_components.climate_advisor import automation

        coord = _coordinator()
        with patch.object(automation, "resolve_active_band", wraps=automation.resolve_active_band) as spy:
            result = active_thresholds_for(coord, now=DAY)
        assert spy.call_count == 1
        assert (result["heat"], result["cool"]) == (68.0, 74.0)

    def test_every_select_comfort_band_branch_labels_a_known_context(self):
        c = _make_classification(hvac_mode="cool")
        seen = set()
        for occ in ("home", "guest", "away", "vacation"):
            for sleep in (False, True):
                for savings in (False, True):
                    for tou in (0.0, 2.0):
                        band = select_comfort_band(
                            c,
                            _CONFIG,
                            occupancy_mode=occ,
                            in_sleep_window=sleep,
                            aggressive_savings=savings,
                            tou_expansion_f=tou,
                        )
                        assert band.context in BAND_CONTEXTS, band
                        seen.add(band.context)
        assert seen == set(BAND_CONTEXTS)
