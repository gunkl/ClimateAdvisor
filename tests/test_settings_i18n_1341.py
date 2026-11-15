"""The Settings tab's temperature-source value as a message (Issue #1341, PR C).

``_temp_source_display`` keeps its English text (the strings the old f-strings built); ``_temp_source_msg`` is the same
text as a catalog message (``settings.temp_source.*``), carried to the dashboard as ``value_msg``.
"""

from __future__ import annotations

import sys
import types

import pytest

if "homeassistant" not in sys.modules:
    from tools.sim_harness.ha_stubs import install_ha_stubs

    install_ha_stubs()

from custom_components.climate_advisor import i18n  # noqa: E402
from custom_components.climate_advisor.api import _temp_source_display, _temp_source_msg  # noqa: E402

CLIMATE = "climate.hall"
DEDICATED = "sensor.indoor_dedicated"


@pytest.fixture(autouse=True)
def _catalogs():
    i18n.reset_catalogs()
    i18n.set_strict(True)
    i18n.load_catalogs()
    yield
    i18n.set_strict(None)
    i18n.reset_catalogs()


def _coord(**kw):
    base = {
        "_indoor_primary_fallback_reason": None,
        "_indoor_companion_in_use": None,
        "_outdoor_sensor_fallback_reason": None,
    }
    base.update(kw)
    return types.SimpleNamespace(**base)


CASES = [
    # (key, source, config, coordinator attrs, the English the old f-strings gave)
    ("indoor_temp_source", "climate_fallback", {}, {}, "Thermostat's built-in sensor"),
    ("indoor_temp_source", "sensor", {}, {}, "Dedicated sensor — none selected (indoor decisions paused)"),
    ("indoor_temp_source", "sensor", {"indoor_temp_entity": DEDICATED}, {}, f"Dedicated sensor — {DEDICATED}"),
    (
        "indoor_temp_source",
        "sensor",
        {"indoor_temp_entity": DEDICATED},
        {"_indoor_primary_fallback_reason": "stale"},
        f"Dedicated sensor — {DEDICATED} (sensor stopped updating — indoor decisions paused)",
    ),
    (
        "indoor_temp_source",
        "climate_fallback",
        {},
        {"_indoor_primary_fallback_reason": "unavailable"},
        "Thermostat's built-in sensor (sensor unavailable — indoor decisions paused)",
    ),
    (
        "indoor_temp_source",
        "climate_fallback",
        {},
        {"_indoor_companion_in_use": "sensor.finer"},
        "Thermostat (finer sensor — sensor.finer)",
    ),
    (
        "indoor_temp_source",
        "climate_fallback",
        {"indoor_temp_entity": DEDICATED},
        {},
        f"Thermostat's built-in sensor — {DEDICATED} selected but not in use",
    ),
    ("outdoor_temp_source", "weather_service", {}, {}, "Weather service"),
    ("outdoor_temp_source", "input_number", {}, {}, "input_number helper — none selected (using weather service)"),
    (
        "outdoor_temp_source",
        "sensor",
        {"outdoor_temp_entity": "sensor.out"},
        {"_outdoor_sensor_fallback_reason": "no_reading"},
        "Dedicated sensor — sensor.out (no reading — using weather service)",
    ),
    (
        "outdoor_temp_source",
        "sensor",
        {"outdoor_temp_entity": "sensor.out"},
        {"_outdoor_sensor_fallback_reason": "some_new_reason"},
        "Dedicated sensor — sensor.out (some new reason — using weather service)",
    ),
]


@pytest.mark.parametrize(("key", "source", "config", "attrs", "english"), CASES)
def test_temp_source_english_is_unchanged_and_the_message_round_trips(key, source, config, attrs, english):
    coord = _coord(**attrs)
    assert _temp_source_display(coord, key, source, "label", config) == english
    msg = _temp_source_msg(coord, key, source, config)
    assert i18n.render(msg, "en") == english
    # the wire form (what the dashboard receives) renders the same
    again = i18n.Msg.from_wire(msg.to_wire())
    assert i18n.render(again, "en") == english


def test_an_unknown_source_has_no_message_and_keeps_its_label():
    assert _temp_source_msg(_coord(), "indoor_temp_source", "mystery", {}) is None
    assert _temp_source_display(_coord(), "indoor_temp_source", "mystery", "mystery", {}) == "mystery"
