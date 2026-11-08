"""Issue #1459: ``chart_data["capabilities"]``, what this zone has set up, for the Status chart key.

Occupant view: a chart-key control locks only when the home cannot have it as set up (no outdoor sensor, no fan, no
door/window sensors, no occupancy switches, no humidity source, v4 switched off). A home that HAS the thing keeps the
control switchable even with nothing in view, so feels-like can be switched on before the weather lines up.

Drives the real ``ClimateAdvisorCoordinator.chart_capabilities`` / ``get_chart_data`` on a partially built coordinator,
the real humidity resolver over the real companion discovery (registry faked) and the real cache that
``_read_chart_humidity`` keeps warm on the event loop.
"""

from __future__ import annotations

import ast
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import pytest  # noqa: E402

from tests.test_chart_humidity_1414 import (  # noqa: E402
    BED_RH,
    BED_TEMP,
    CHOSEN,
    CLIMATE,
    NOW,
    OUT_RH,
    OUT_TEMP,
    PKG,
    REGISTRY,
    WEATHER,
    _coord_mod,
    _fake_er,
    _get_coordinator_class,
    _hass,
    _state,
)

KEYS = (
    "v4_running",
    "outdoor_sensor",
    "fan",
    "door_sensors",
    "occupancy",
    "humidity_indoor",
    "humidity_outdoor",
)


@pytest.fixture
def registry():
    with patch("custom_components.climate_advisor.indoor_temp.er", _fake_er(REGISTRY)):
        yield


def _coord(states: dict | None = None, **cfg):
    cls = _get_coordinator_class()
    coord = object.__new__(cls)
    coord.hass = _hass(states or {})
    coord.config = {
        "climate_entity": CLIMATE,
        "temp_unit": "fahrenheit",
        **cfg,
    }
    coord._async_save_state = AsyncMock()
    ae = MagicMock()
    ae._natural_vent_active = False
    ae._fan_override_active = False
    ae._manual_override_active = False
    ae._grace_active = False
    coord.automation_engine = ae
    coord._companion_sensors_cache = {}
    coord.chart_capabilities = types.MethodType(cls.chart_capabilities, coord)
    return coord


def _caps(coord) -> dict[str, bool]:
    return coord.chart_capabilities()


def _warm(coord, *, sleep: bool) -> None:
    """One chart row on the event loop (the real ``_read_chart_humidity``), which fills the companion cache."""
    with patch.object(_coord_mod(), "_in_sleep_window", return_value=sleep):
        coord._read_chart_humidity(indoor_f=75.0, outdoor_f=80.0)


# ── each flag, true and false ────────────────────────────────────────────────────────────────────────────────────────
def test_bare_zone_has_nothing_but_v4():
    caps = _caps(_coord())
    assert set(caps) == set(KEYS)
    assert caps == {k: k == "v4_running" for k in KEYS}  # v4 is the default primary engine


@pytest.mark.parametrize(
    ("cfg", "expected"),
    [
        ({}, True),
        ({"primary_engine": "v4"}, True),
        ({"primary_engine": "v3", "shadow_engine": "v4"}, True),
        ({"primary_engine": "v3", "shadow_engine": "none"}, False),
        ({"primary_engine": "v3"}, False),
    ],
)
def test_v4_running(cfg, expected):
    assert _caps(_coord(**cfg))["v4_running"] is expected


@pytest.mark.parametrize(
    ("cfg", "expected"),
    [
        ({"outdoor_temp_source": "sensor", "outdoor_temp_entity": OUT_TEMP}, True),
        ({"outdoor_temp_source": "input_number", "outdoor_temp_entity": "input_number.outdoor"}, True),
        ({"outdoor_temp_source": "sensor"}, False),  # a sensor source with no entity picked
        ({"outdoor_temp_source": "weather_service", "outdoor_temp_entity": OUT_TEMP}, False),
        ({}, False),
    ],
)
def test_outdoor_sensor(cfg, expected):
    assert _caps(_coord(**cfg))["outdoor_sensor"] is expected


@pytest.mark.parametrize(
    ("cfg", "expected"),
    [
        ({"fan_mode": "hvac_fan"}, True),  # the thermostat's own blower: no entity needed
        ({"fan_mode": "whole_house_fan", "fan_entity": "fan.whf"}, True),
        ({"fan_mode": "whole_house_fan"}, False),  # a WHF mode with no fan entity cannot run a fan
        ({"fan_mode": "both", "fan_entity": "switch.whf"}, True),
        ({"fan_mode": "disabled", "fan_entity": "fan.whf"}, False),
        ({"fan_mode": "none"}, False),
        ({}, False),
    ],
)
def test_fan(cfg, expected):
    assert _caps(_coord(**cfg))["fan"] is expected


@pytest.mark.parametrize(
    ("sensors", "expected"),
    [(["binary_sensor.patio_door"], True), (["binary_sensor.a", "binary_sensor.b"], True), ([], False), (None, False)],
)
def test_door_sensors(sensors, expected):
    cfg = {} if sensors is None else {"door_window_sensors": sensors}
    assert _caps(_coord(**cfg))["door_sensors"] is expected


@pytest.mark.parametrize(
    ("cfg", "expected"),
    [
        ({"home_toggle_entity": "input_boolean.home"}, True),
        ({"vacation_toggle_entity": "input_boolean.vacation"}, True),
        ({"guest_toggle_entity": "input_boolean.guest"}, True),
        ({"home_toggle_entity": "", "vacation_toggle_entity": None}, False),
        ({}, False),
    ],
)
def test_occupancy(cfg, expected):
    assert _caps(_coord(**cfg))["occupancy"] is expected


class TestHumidityIndoor:
    def test_chosen_entity(self, registry):
        coord = _coord({CHOSEN: _state("47.0")}, indoor_humidity_entity=CHOSEN)
        assert _caps(coord)["humidity_indoor"] is True

    def test_thermostat_current_humidity(self, registry):
        coord = _coord({CLIMATE: _state("cool", {"current_temperature": 75.0, "current_humidity": 41.0})})
        assert _caps(coord)["humidity_indoor"] is True

    def test_target_humidity_is_not_a_source(self, registry):
        coord = _coord({CLIMATE: _state("cool", {"current_temperature": 75.0, "humidity": 45.0})})
        assert _caps(coord)["humidity_indoor"] is False

    def test_nothing(self, registry):
        coord = _coord({CLIMATE: _state("cool", {"current_temperature": 75.0})}, sleep_indoor_temp_entity=BED_TEMP)
        assert _caps(coord)["humidity_indoor"] is False

    def test_bedroom_device_sensor_counts_in_the_daytime(self, registry, frozen_now):
        """The bedroom's humidity sensor (only on the bedroom sensor's device) is found from a DAY row's warm-up."""
        frozen_now(NOW)
        states = {
            CLIMATE: _state("cool", {"current_temperature": 75.0}),
            BED_TEMP: _state("71.2"),
            BED_RH: _state("61.7"),
        }
        coord = _coord(states, sleep_indoor_temp_entity=BED_TEMP)
        assert _caps(coord)["humidity_indoor"] is False  # nothing looked up on the event loop yet: cache-only
        _warm(coord, sleep=False)
        assert _caps(coord)["humidity_indoor"] is True

    def test_device_lookup_never_touches_the_registry_off_the_loop(self, frozen_now):
        frozen_now(NOW)
        states = {CLIMATE: _state("cool", {"current_temperature": 75.0}), BED_RH: _state("61.7")}
        coord = _coord(states, sleep_indoor_temp_entity=BED_TEMP)
        boom = MagicMock(side_effect=AssertionError("registry read off the event loop"))
        with patch("custom_components.climate_advisor.indoor_temp.er", MagicMock(async_get=boom)):
            assert _caps(coord)["humidity_indoor"] is False
        boom.assert_not_called()


class TestHumidityOutdoor:
    def test_weather_humidity(self, registry):
        coord = _coord({WEATHER: _state("sunny", {"humidity": 33.0})}, weather_entity=WEATHER)
        assert _caps(coord)["humidity_outdoor"] is True

    def test_chosen_entity(self, registry):
        coord = _coord({OUT_RH: _state("58.25")}, outdoor_humidity_entity=OUT_RH)
        assert _caps(coord)["humidity_outdoor"] is True

    def test_outdoor_sensor_device(self, registry, frozen_now):
        frozen_now(NOW)
        states = {
            CLIMATE: _state("cool", {"current_temperature": 75.0}),
            WEATHER: _state("sunny", {}),
            OUT_TEMP: _state("92.0"),
            OUT_RH: _state("58.25"),
        }
        coord = _coord(states, weather_entity=WEATHER, outdoor_temp_source="sensor", outdoor_temp_entity=OUT_TEMP)
        _warm(coord, sleep=False)
        assert _caps(coord)["humidity_outdoor"] is True

    def test_nothing(self, registry):
        coord = _coord({WEATHER: _state("sunny", {})}, weather_entity=WEATHER)
        assert _caps(coord)["humidity_outdoor"] is False

    def test_indoor_humidity_never_counts_outdoors(self, registry):
        coord = _coord({CLIMATE: _state("cool", {"current_humidity": 41.0})}, weather_entity=WEATHER)
        caps = _caps(coord)
        assert caps["humidity_indoor"] is True and caps["humidity_outdoor"] is False


# ── an internal error falls back to True (nothing locks), per key ────────────────────────────────────────────────────
def test_a_failing_check_is_true_and_the_others_stand():
    coord = _coord()
    coord._resolve_monitored_sensors = MagicMock(side_effect=RuntimeError("boom"))
    with patch.object(_coord_mod().prediction_engines, "is_running", side_effect=RuntimeError("boom")):
        caps = _caps(coord)
    assert caps["door_sensors"] is True and caps["v4_running"] is True
    assert caps["fan"] is False and caps["occupancy"] is False and caps["outdoor_sensor"] is False


def test_a_broken_config_is_all_true():
    coord = _coord()
    coord.config = MagicMock(get=MagicMock(side_effect=RuntimeError("boom")))
    assert _caps(coord) == {k: True for k in KEYS}


# ── the block is always on chart_data ────────────────────────────────────────────────────────────────────────────────
def _chart(*, historical: bool, **cfg) -> dict:
    from datetime import UTC, timedelta

    from tests.test_forecast_chart_integration import _NOW, _make_full_chart_coord, _patched_dt_util

    coord = _make_full_chart_coord(daily_forecast_full=None)
    coord.config.update(cfg)
    coord.hass = _hass({})
    coord._async_save_state = AsyncMock()
    now_p, as_local_p = _patched_dt_util(_coord_mod(), now=_NOW.replace(tzinfo=UTC) if historical else _NOW)
    with now_p, as_local_p:
        if historical:
            return coord.get_chart_data("24h", before_ts=(_NOW - timedelta(days=3)).replace(tzinfo=UTC).timestamp())
        return coord.get_chart_data("24h")


@pytest.mark.parametrize("historical", [False, True])
def test_chart_data_always_carries_the_block(historical):
    res = _chart(
        historical=historical,
        fan_mode="hvac_fan",
        door_window_sensors=["binary_sensor.patio_door"],
        primary_engine="v3",
    )
    caps = res["capabilities"]
    assert set(caps) == set(KEYS)
    assert caps["fan"] is True and caps["door_sensors"] is True and caps["v4_running"] is False
    assert caps["occupancy"] is False and caps["outdoor_sensor"] is False


def test_chart_data_carries_the_block_when_every_check_fails():
    with patch.object(_coord_mod().prediction_engines, "is_running", side_effect=RuntimeError("boom")):
        res = _chart(historical=False)
    assert res["capabilities"]["v4_running"] is True


def test_only_get_chart_data_calls_it():
    src = (PKG / "coordinator.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ClimateAdvisorCoordinator")
    callers = {
        fn.name
        for fn in cls.body
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
        for n in ast.walk(fn)
        if isinstance(n, ast.Attribute) and n.attr == "chart_capabilities"
    }
    assert callers == {"get_chart_data"}  # display only: no decision path reads it
