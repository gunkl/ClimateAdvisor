"""Issue #1230 R1b ("Bug D"): control reads the thermostat's finer companion sensor when proven.

Occupant view: the hallway thermostat reports 69 F in whole degrees while its device also has a 68.54 F sensor. Once
that sensor has proven it is the same reading at finer resolution, comfort decisions see 68.54 instead of 69, so Climate
Advisor reacts to the real temperature. A thermostat without such a sensor, or any doubt about it, behaves exactly as
before. These tests pin the state matrix and the latch; the coordinator path and the engine path must agree.
"""

from __future__ import annotations

import datetime as _dt
import itertools
import sys
import types
from unittest.mock import MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import pytest  # noqa: E402

from custom_components.climate_advisor.automation import AutomationEngine  # noqa: E402
from custom_components.climate_advisor.const import INDOOR_SENSOR_STALE_HOURS  # noqa: E402
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402
from custom_components.climate_advisor.indoor_temp import (  # noqa: E402
    CompanionSensors,
    resolve_indoor_temp_with_provenance,
)

NOW = _dt.datetime(2026, 11, 6, 12, 0, tzinfo=_dt.UTC)
CLIMATE = "climate.hallway"
COMP = "sensor.hallway_current_temperature"
COMP2 = "sensor.hallway_temperature_2"
DEDICATED = "sensor.dedicated_indoor"
SLEEP = "sensor.bedroom_temp"
SLEEP_F = 66.4
DEDICATED_F = 70.2


def _state(value, attributes=None, age_h=None):
    s = MagicMock()
    s.state = value
    s.attributes = attributes or {}
    age = _dt.timedelta(hours=age_h or 0)
    s.last_reported = NOW - age
    s.last_updated = NOW - age
    return s


def _hass(states):
    hass = MagicMock()
    hass.states.get = lambda eid: states.get(eid)
    return hass


def _states(climate_attr, comp):
    """comp: scenario name -> {entity: state} for the companion(s)."""
    out = {
        CLIMATE: _state("heat", {"current_temperature": climate_attr}),
        DEDICATED: _state(str(DEDICATED_F)),
        SLEEP: _state(str(SLEEP_F)),
    }
    if comp == "fine":
        out[COMP] = _state("68.54")
    elif comp == "inconsistent":
        out[COMP] = _state("71.9")
    elif comp == "unavailable":
        out[COMP] = _state("unavailable")
    elif comp == "stale":
        out[COMP] = _state("68.54", age_h=INDOOR_SENSOR_STALE_HOURS + 1)
    elif comp == "two_both_ok":
        out[COMP] = _state("68.54")
        out[COMP2] = _state("68.6")
    elif comp == "two_one_ok":
        out[COMP] = _state("68.54")
        out[COMP2] = _state("unavailable")
    return out


def _entities(comp):
    return {
        "none": (),
        "two_both_ok": (COMP, COMP2),
        "two_one_ok": (COMP, COMP2),
    }.get(comp, (COMP,))


def _coordinator(states, companion, *, source, setting, sleep_on, unit="fahrenheit"):
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = _hass(states)
    coord.config = {
        "climate_entity": CLIMATE,
        "indoor_temp_source": source,
        "indoor_temp_entity": DEDICATED,
        "sleep_indoor_temp_entity": SLEEP,
        "temp_unit": unit,
        "thermostat_companion_sensor": setting,
    }
    coord._companion = companion
    coord._sleep_on = sleep_on
    return coord


def _engine(states, companion, *, source, setting, sleep_on, unit="fahrenheit"):
    ae = object.__new__(AutomationEngine)
    ae.hass = _hass(states)
    ae.climate_entity = CLIMATE
    ae.config = {
        "climate_entity": CLIMATE,
        "indoor_temp_source": source,
        "indoor_temp_entity": DEDICATED,
        "sleep_indoor_temp_entity": SLEEP,
        "temp_unit": unit,
        "thermostat_companion_sensor": setting,
    }
    ae.indoor_companion = companion
    return ae


def _both_readings(states, entities, *, source, setting, sleep_on, unit="fahrenheit", frozen_now=None):
    companion = CompanionSensors(entities)
    coord = _coordinator(states, companion, source=source, setting=setting, sleep_on=sleep_on, unit=unit)
    engine = _engine(states, companion, source=source, setting=setting, sleep_on=sleep_on, unit=unit)
    with (
        patch("custom_components.climate_advisor.coordinator._in_sleep_window", return_value=sleep_on),
        patch("custom_components.climate_advisor.automation._in_sleep_window", return_value=sleep_on),
        patch("custom_components.climate_advisor.indoor_temp.dt_util.utcnow", return_value=NOW),
    ):
        return coord._get_indoor_temp_with_provenance(), engine._get_indoor_reading(), engine._get_indoor_temp_f()


def _expected(source, comp, climate_attr, setting, sleep_on):
    """(value, source_entity, primary_value, companion_entity)"""
    if source == "sensor":
        primary, primary_entity = DEDICATED_F, DEDICATED
        comp_used = None
    else:
        primary, primary_entity, comp_used = float(climate_attr), CLIMATE, None
        if setting == "auto" and comp in ("fine", "two_one_ok") and climate_attr == 69:
            primary, primary_entity, comp_used = 68.54, COMP, COMP
    if sleep_on:
        return SLEEP_F, SLEEP, primary, comp_used
    return primary, primary_entity, primary, comp_used


MATRIX = list(
    itertools.product(
        ["climate_fallback", "sensor"],
        ["none", "fine", "inconsistent", "unavailable", "stale", "two_both_ok", "two_one_ok"],
        [69, 68.5],
        ["auto", "off"],
        [False, True],
    )
)


@pytest.mark.parametrize(("source", "comp", "climate_attr", "setting", "sleep_on"), MATRIX)
def test_state_matrix_coordinator_and_engine_agree(source, comp, climate_attr, setting, sleep_on):
    states = _states(climate_attr, comp)
    coord_r, engine_r, engine_value = _both_readings(
        states, _entities(comp), source=source, setting=setting, sleep_on=sleep_on
    )
    value, src, primary, comp_used = _expected(source, comp, climate_attr, setting, sleep_on)
    for reading in (coord_r, engine_r):
        assert reading.value == pytest.approx(value)
        assert reading.source_entity == src
        assert reading.primary_value == pytest.approx(primary)
        assert reading.companion_entity == comp_used
    assert engine_value == pytest.approx(value)
    # The coordinator path and the engine path are the same answer (the engine's decisions use the same value).
    assert tuple(coord_r) == tuple(engine_r)


def test_matrix_size():
    assert len(MATRIX) == 112


# ---------------------------------------------------------------------------
# Reasons (provenance)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("comp", "climate_attr", "reason"),
    [
        ("inconsistent", 69, "inconsistent"),
        ("unavailable", 69, "unavailable"),
        ("stale", 69, "stale"),
        ("two_both_ok", 69, "ambiguous"),
        ("fine", 68.5, "not_proven"),
        ("fine", 69, None),
    ],
)
def test_companion_reason(comp, climate_attr, reason):
    reading, _, _ = _both_readings(
        _states(climate_attr, comp), _entities(comp), source="climate_fallback", setting="auto", sleep_on=False
    )
    assert reading.companion_reason == reason


def test_no_candidates_means_no_reason_and_unchanged_reading():
    reading, _, _ = _both_readings(_states(69, "none"), (), source="climate_fallback", setting="auto", sleep_on=False)
    assert (reading.value, reading.companion_entity, reading.companion_reason) == (69.0, None, None)


def test_implausible_companion_falls_back():
    states = _states(69, "fine")
    states[COMP] = _state("12.5")
    reading, _, _ = _both_readings(states, (COMP,), source="climate_fallback", setting="auto", sleep_on=False)
    assert reading.value == 69.0 and reading.companion_reason == "implausible"


def test_climate_unreadable_keeps_todays_failure():
    states = _states(69, "fine")
    states[CLIMATE] = _state("unavailable", {})
    reading, _, _ = _both_readings(states, (COMP,), source="climate_fallback", setting="auto", sleep_on=False)
    assert reading.value is None and reading.reason is not None and reading.companion_entity is None


# ---------------------------------------------------------------------------
# The latch
# ---------------------------------------------------------------------------


def _resolve(states, companion):
    return resolve_indoor_temp_with_provenance(
        hass=_hass(states),
        source="climate_fallback",
        unit="fahrenheit",
        indoor_temp_entity=None,
        climate_entity=CLIMATE,
        now=NOW,
        companion=companion,
    )


class TestLatch:
    def test_not_used_until_a_fraction_is_seen_beside_a_whole_reading_then_stays(self):
        companion = CompanionSensors((COMP,))
        # Companion reads a whole number: no evidence it is finer yet.
        r = _resolve({CLIMATE: _state("heat", {"current_temperature": 69}), COMP: _state("69.0")}, companion)
        assert (r.value, r.companion_entity, r.companion_reason) == (69.0, None, "not_proven")
        assert not companion.proven
        # A fraction appears while the thermostat shows a whole degree: latched and used from this reading on.
        r = _resolve({CLIMATE: _state("heat", {"current_temperature": 69}), COMP: _state("68.54")}, companion)
        assert (r.value, r.companion_entity) == (68.54, COMP)
        assert companion.proven == {COMP}
        # Back to a whole companion reading: still used (latched, no flapping).
        r = _resolve({CLIMATE: _state("heat", {"current_temperature": 69}), COMP: _state("69.0")}, companion)
        assert (r.value, r.companion_entity) == (69.0, COMP)

    def test_fractional_climate_attribute_never_latches(self):
        companion = CompanionSensors((COMP,))
        r = _resolve({CLIMATE: _state("heat", {"current_temperature": 68.5}), COMP: _state("68.54")}, companion)
        assert r.companion_entity is None and r.value == 68.5
        assert not companion.proven

    def test_inconsistent_fraction_never_latches(self):
        companion = CompanionSensors((COMP,))
        r = _resolve({CLIMATE: _state("heat", {"current_temperature": 69}), COMP: _state("72.3")}, companion)
        assert r.companion_entity is None and not companion.proven

    def test_latched_companion_still_checked_for_consistency_each_reading(self):
        companion = CompanionSensors((COMP,))
        _resolve({CLIMATE: _state("heat", {"current_temperature": 69}), COMP: _state("68.54")}, companion)
        r = _resolve({CLIMATE: _state("heat", {"current_temperature": 69}), COMP: _state("73.4")}, companion)
        assert (r.value, r.companion_reason) == (69.0, "inconsistent")
        r = _resolve({CLIMATE: _state("heat", {"current_temperature": 69}), COMP: _state("68.7")}, companion)
        assert (r.value, r.companion_entity) == (68.7, COMP)

    def test_no_companion_object_is_the_old_behavior(self):
        r = _resolve({CLIMATE: _state("heat", {"current_temperature": 69}), COMP: _state("68.54")}, None)
        assert (r.value, r.source_entity, r.companion_entity, r.companion_reason) == (69.0, CLIMATE, None, None)


def test_celsius_install():
    """20 C whole on the thermostat, 20.3 C companion: used, and stored/returned as F."""
    states = {
        CLIMATE: _state("heat", {"current_temperature": 20}),
        COMP: _state("20.3"),
    }
    reading, engine_r, _ = _both_readings(
        states, (COMP,), source="climate_fallback", setting="auto", sleep_on=False, unit="celsius"
    )
    assert reading.value == pytest.approx(68.54, abs=0.005)
    assert reading.companion_entity == COMP and tuple(reading) == tuple(engine_r)


def test_celsius_consistency_uses_degrees_f():
    states = {CLIMATE: _state("heat", {"current_temperature": 20}), COMP: _state("21.2")}  # 70.16 F vs 68 F
    reading, _, _ = _both_readings(
        states, (COMP,), source="climate_fallback", setting="auto", sleep_on=False, unit="celsius"
    )
    assert reading.companion_reason == "inconsistent" and reading.value == pytest.approx(68.0)


# ---------------------------------------------------------------------------
# Coordinator funnel: announce once per change, label, no flapping
# ---------------------------------------------------------------------------


def _funnel_coord(states, companion):
    coord = _coordinator(states, companion, source="climate_fallback", setting="auto", sleep_on=False)
    coord._startup_coalesce_active = False
    coord._refresh_indoor_sensor_health  # noqa: B018 - bound method exists
    return coord


class TestFunnel:
    def _tick(self, coord, caplog):
        caplog.clear()
        with (
            patch("custom_components.climate_advisor.coordinator._in_sleep_window", return_value=False),
            patch("custom_components.climate_advisor.indoor_temp.dt_util.utcnow", return_value=NOW),
            patch.object(ClimateAdvisorCoordinator, "_emit_event", MagicMock()),
        ):
            coord._refresh_indoor_sensor_health()
        return [r.getMessage() for r in caplog.records if r.levelname == "INFO" and "finer sensor" in r.getMessage()]

    def test_announces_once_per_transition_and_never_per_call(self, caplog):
        import logging

        caplog.set_level(logging.DEBUG)
        companion = CompanionSensors((COMP,))
        states = {CLIMATE: _state("heat", {"current_temperature": 69}), COMP: _state("68.54")}
        coord = _funnel_coord(states, companion)
        assert len(self._tick(coord, caplog)) == 1  # companion enters use
        assert self._tick(coord, caplog) == []  # steady: silent
        assert self._tick(coord, caplog) == []
        assert coord._indoor_companion_in_use == COMP
        states[COMP] = _state("unavailable")
        msgs = self._tick(coord, caplog)  # companion -> thermostat reading
        assert len(msgs) == 1 and "back to the thermostat" in msgs[0]
        assert self._tick(coord, caplog) == []  # still out: silent, even if the reason changes
        states[COMP] = _state("68.6", age_h=INDOOR_SENSOR_STALE_HOURS + 1)
        assert self._tick(coord, caplog) == []
        states[COMP] = _state("68.6")
        msgs = self._tick(coord, caplog)  # and back: exactly one more
        assert len(msgs) == 1 and "changed to the thermostat's finer sensor" in msgs[0]
        assert coord._indoor_companion_in_use == COMP

    def test_nothing_announced_during_startup_coalesce_then_once(self, caplog):
        import logging

        caplog.set_level(logging.DEBUG)
        states = {CLIMATE: _state("heat", {"current_temperature": 69}), COMP: _state("68.54")}
        coord = _funnel_coord(states, CompanionSensors((COMP,)))
        coord._startup_coalesce_active = True
        assert self._tick(coord, caplog) == []
        assert coord._indoor_companion_in_use == COMP  # the label already reflects it
        coord._startup_coalesce_active = False
        assert len(self._tick(coord, caplog)) == 1
        assert self._tick(coord, caplog) == []

    def test_ambiguity_noted_once(self, caplog):
        import logging

        caplog.set_level(logging.DEBUG)
        states = _states(69, "two_both_ok")
        coord = _funnel_coord(states, CompanionSensors((COMP, COMP2)))
        for _ in range(3):
            self._tick(coord, caplog)
        ambiguous = [r for r in caplog.records if "several temperature sensors" in r.getMessage()]
        assert coord._indoor_companion_in_use is None
        assert len(ambiguous) <= 1


def test_settings_label_names_the_companion():
    from custom_components.climate_advisor.api import _temp_source_display

    coord = types.SimpleNamespace(_indoor_primary_fallback_reason=None, _indoor_companion_in_use=COMP)
    text = _temp_source_display(
        coord, "indoor_temp_source", "climate_fallback", "Thermostat's built-in sensor", {"climate_entity": CLIMATE}
    )
    assert text == f"Thermostat (finer sensor — {COMP})"
    coord._indoor_companion_in_use = None
    assert (
        _temp_source_display(
            coord, "indoor_temp_source", "climate_fallback", "Thermostat's built-in sensor", {"climate_entity": CLIMATE}
        )
        == "Thermostat's built-in sensor"
    )
    # A dedicated sensor source is never relabelled.
    assert "finer" not in _temp_source_display(
        types.SimpleNamespace(_indoor_primary_fallback_reason=None, _indoor_companion_in_use=COMP),
        "indoor_temp_source",
        "sensor",
        "Dedicated sensor",
        {"indoor_temp_entity": DEDICATED},
    )


def test_swing_resolution_and_primary_entity_follow_the_companion():
    states = _states(69, "fine")
    coord = _coordinator(states, CompanionSensors((COMP,)), source="climate_fallback", setting="auto", sleep_on=False)
    with (
        patch("custom_components.climate_advisor.coordinator._in_sleep_window", return_value=False),
        patch("custom_components.climate_advisor.indoor_temp.dt_util.utcnow", return_value=NOW),
    ):
        assert coord._primary_indoor_entity() == COMP
        assert coord._swing_reading_resolution_f() == 0.0
    off = _coordinator(states, CompanionSensors((COMP,)), source="climate_fallback", setting="off", sleep_on=False)
    with (
        patch("custom_components.climate_advisor.coordinator._in_sleep_window", return_value=False),
        patch("custom_components.climate_advisor.indoor_temp.dt_util.utcnow", return_value=NOW),
    ):
        assert off._primary_indoor_entity() == CLIMATE
        assert off._swing_reading_resolution_f() == 1.0
