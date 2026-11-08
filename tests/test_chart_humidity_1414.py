"""Issue #1414: indoor and outdoor humidity and feels-like on the Status charts.

Occupant view: the chart shows how humid the room Climate Advisor reads is and, on a hot humid afternoon, how hot it
feels (the NWS heat index); outside it also shows the wind chill on a cold windy morning. The bedroom's humidity is
shown while the bedroom sensor is the one in use, never the hallway's. A mild day shows humidity but no feels-like
line. None of it changes what the thermostat is told: chart display only.

Drives the real resolver (``humidity_sources.resolve_humidity``) over the real companion discovery
(``indoor_temp.discover_companion_sensors``, registry faked), the real coordinator methods (``_read_chart_humidity``,
``_read_chart_context``, ``_get_indoor_temp_with_provenance``, ``get_chart_data``) and the real ``ChartStateLog``.
"""

from __future__ import annotations

import ast
import datetime as _dt
import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import pytest  # noqa: E402

from custom_components.climate_advisor.humidity_sources import (  # noqa: E402
    ROLE_INDOOR_DAY,
    ROLE_INDOOR_SLEEP,
    ROLE_OUTDOOR,
    indoor_role_for,
    read_humidity_state,
    resolve_humidity,
)

NOW = _dt.datetime(2026, 7, 14, 15, 0, tzinfo=_dt.UTC)
CLIMATE = "climate.hallway"
WEATHER = "weather.home"
HALL_RH = "sensor.hallway_humidity"
HALL_TEMP = "sensor.hallway_temp"
HALL_RH2 = "sensor.hallway_temp_humidity"
BED_TEMP = "sensor.bedroom_temp"
BED_RH = "sensor.bedroom_humidity"
OUT_TEMP = "sensor.patio_temp"
OUT_RH = "sensor.patio_humidity"
THERMO_OUT_RH = "sensor.hallway_outdoor_humidity"
CHOSEN = "sensor.chosen_humidity"
PKG = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"


def _get_coordinator_class():
    return importlib.import_module("custom_components.climate_advisor.coordinator").ClimateAdvisorCoordinator


def _coord_mod():
    return importlib.import_module("custom_components.climate_advisor.coordinator")


def _state(value, attributes: dict | None = None, age_h: float = 0.1) -> MagicMock:
    s = MagicMock()
    s.state = value
    s.attributes = attributes or {}
    s.last_reported = NOW - _dt.timedelta(hours=age_h)
    s.last_updated = NOW - _dt.timedelta(hours=age_h)
    return s


def _hass(states: dict) -> MagicMock:
    hass = MagicMock()
    hass.states.get = lambda eid: states.get(eid)
    hass.async_create_task = MagicMock(side_effect=lambda c: c.close())
    return hass


def _reg(entity_id, device_id, device_class=None, original_name=None):
    return SimpleNamespace(
        entity_id=entity_id,
        domain=entity_id.split(".")[0],
        device_id=device_id,
        device_class=None,
        original_device_class=device_class,
        original_name=original_name,
        translation_key=None,
    )


# The house's devices: the thermostat (with its own humidity sensor and an "Outdoor humidity" one, as some thermostats
# have), the bedroom sensor, the patio sensor.
REGISTRY = {
    CLIMATE: _reg(CLIMATE, "thermostat"),
    HALL_RH: _reg(HALL_RH, "thermostat", "humidity", "Humidity"),
    THERMO_OUT_RH: _reg(THERMO_OUT_RH, "thermostat", "humidity", "Outdoor humidity"),
    HALL_TEMP: _reg(HALL_TEMP, "hall_sensor", "temperature"),
    HALL_RH2: _reg(HALL_RH2, "hall_sensor", "humidity"),
    BED_TEMP: _reg(BED_TEMP, "bedroom", "temperature"),
    BED_RH: _reg(BED_RH, "bedroom", "humidity"),
    OUT_TEMP: _reg(OUT_TEMP, "patio", "temperature"),
    OUT_RH: _reg(OUT_RH, "patio", "humidity"),
    "number.hallway_target_humidity": _reg("number.hallway_target_humidity", "thermostat", "humidity"),
}


def _fake_er(entries: dict):
    reg = SimpleNamespace(async_get=lambda eid: entries.get(eid))

    def siblings(_reg, device_id, include_disabled_entities=True):
        assert include_disabled_entities is False
        return [e for e in entries.values() if e.device_id == device_id]

    return SimpleNamespace(async_get=lambda _hass: reg, async_entries_for_device=siblings)


@pytest.fixture
def registry():
    with patch("custom_components.climate_advisor.indoor_temp.er", _fake_er(REGISTRY)):
        yield


def _house(**over) -> dict:
    states = {
        CLIMATE: _state("cool", {"current_temperature": 76.0, "current_humidity": 41.0, "humidity": 99.0}),
        WEATHER: _state("sunny", {"humidity": 33.0, "wind_speed": 16.09344, "wind_speed_unit": "km/h"}),
        HALL_RH: _state("52.4"),
        THERMO_OUT_RH: _state("88.0"),
        HALL_TEMP: _state("75.5"),
        HALL_RH2: _state("50.0"),
        BED_TEMP: _state("71.2"),
        BED_RH: _state("61.7"),
        OUT_TEMP: _state("92.0"),
        OUT_RH: _state("58.25"),
        CHOSEN: _state("47.0"),
        "number.hallway_target_humidity": _state("45"),
    }
    states.update(over)
    return {k: v for k, v in states.items() if v is not None}


# ── the resolver: source order per role ──────────────────────────────────────────────────────────────────────────────
def _resolve(role, states, **kw):
    return resolve_humidity(_hass(states), role, **kw)


class TestIndoorDayOrder:
    KW = {"source_entities": (CLIMATE, HALL_TEMP), "fallback_entity": CLIMATE}

    def test_chosen_first(self, registry):
        assert _resolve(ROLE_INDOOR_DAY, _house(), chosen=CHOSEN, **self.KW) == (47.0, CHOSEN)

    def test_climate_device_sensor_second(self, registry):
        assert _resolve(ROLE_INDOOR_DAY, _house(), **self.KW) == (52.4, HALL_RH)

    def test_comfort_source_device_sensor_next(self, registry):
        assert _resolve(ROLE_INDOOR_DAY, _house(**{HALL_RH: None}), **self.KW) == (50.0, HALL_RH2)

    def test_climate_current_humidity_third(self, registry):
        st = _house(**{HALL_RH: None, HALL_RH2: None})
        assert _resolve(ROLE_INDOOR_DAY, st, **self.KW) == (41.0, f"{CLIMATE}#current_humidity")

    def test_unusable_chosen_falls_through(self, registry):
        for bad in ("unavailable", "120", "-3", "nan", "inf"):
            st = _house(**{CHOSEN: _state(bad)})
            assert _resolve(ROLE_INDOOR_DAY, st, chosen=CHOSEN, **self.KW) == (52.4, HALL_RH)

    def test_chosen_is_trusted_at_native_resolution(self, registry):
        st = _house(**{CHOSEN: _state("47.183")})
        assert _resolve(ROLE_INDOOR_DAY, st, chosen=CHOSEN, **self.KW)[0] == 47.183

    def test_never_target_attribute_weather_outdoor_named_or_number(self, registry):
        """With every honest indoor source gone, nothing at all: never the climate TARGET 'humidity' (99), never the
        weather's 33, never the thermostat's own 'Outdoor humidity' sensor (88), never number.* (45)."""
        st = _house(**{HALL_RH: None, HALL_RH2: None})
        st[CLIMATE] = _state("cool", {"current_temperature": 76.0, "humidity": 99.0})
        assert _resolve(ROLE_INDOOR_DAY, st, **self.KW) == (None, None)
        # a weather entity passed as the fallback of an indoor role is refused by the role's fixed domain
        assert _resolve(ROLE_INDOOR_DAY, st, source_entities=(), fallback_entity=WEATHER) == (None, None)
        assert _resolve(ROLE_INDOOR_DAY, st, chosen="number.hallway_target_humidity", **self.KW) == (None, None)


class TestIndoorSleepOrder:
    KW = {"source_entities": (BED_TEMP,)}

    def test_chosen_first(self, registry):
        assert _resolve(ROLE_INDOOR_SLEEP, _house(), chosen=CHOSEN, **self.KW) == (47.0, CHOSEN)

    def test_bedroom_device_sensor_second(self, registry):
        assert _resolve(ROLE_INDOOR_SLEEP, _house(), **self.KW) == (61.7, BED_RH)

    def test_no_third_source(self, registry):
        """No bedroom humidity -> nothing, even with the climate entity passed as a fallback."""
        st = _house(**{BED_RH: None})
        assert _resolve(ROLE_INDOOR_SLEEP, st, fallback_entity=CLIMATE, **self.KW) == (None, None)


class TestOutdoorOrder:
    KW = {"source_entities": (OUT_TEMP,), "fallback_entity": WEATHER}

    def test_chosen_first(self, registry):
        assert _resolve(ROLE_OUTDOOR, _house(), chosen=CHOSEN, **self.KW) == (47.0, CHOSEN)

    def test_outdoor_sensor_device_second(self, registry):
        assert _resolve(ROLE_OUTDOOR, _house(), **self.KW) == (58.25, OUT_RH)

    def test_weather_humidity_third(self, registry):
        assert _resolve(ROLE_OUTDOOR, _house(**{OUT_RH: None}), **self.KW) == (33.0, f"{WEATHER}#humidity")

    def test_outdoor_named_sensor_allowed_outdoors(self, registry):
        """An 'Outdoor humidity' sensor is excluded only from the INDOOR roles' auto-detection."""
        st = _house(**{HALL_RH: None})
        assert _resolve(ROLE_OUTDOOR, st, source_entities=(CLIMATE,), fallback_entity=WEATHER) == (
            88.0,
            THERMO_OUT_RH,
        )

    def test_climate_entity_never_the_outdoor_fallback(self, registry):
        st = _house(**{OUT_RH: None})
        assert _resolve(ROLE_OUTDOOR, st, source_entities=(OUT_TEMP,), fallback_entity=CLIMATE) == (None, None)


def test_role_pairing_and_reader_rules():
    assert indoor_role_for(BED_TEMP, BED_TEMP) == ROLE_INDOOR_SLEEP
    assert indoor_role_for(CLIMATE, BED_TEMP) == ROLE_INDOOR_DAY
    assert indoor_role_for(None, None) == ROLE_INDOOR_DAY
    hass = _hass({"sensor.x": _state("100"), "sensor.y": _state("0"), "number.z": _state("50")})
    assert read_humidity_state(hass, "sensor.x") == 100.0
    assert read_humidity_state(hass, "sensor.y") == 0.0
    assert read_humidity_state(hass, "number.z") is None
    assert resolve_humidity(hass, "bogus") == (None, None)


# ── the coordinator: which room's humidity a row gets (day / sleep), and the feels-like ──────────────────────────────
def _coord(states: dict, **cfg):
    cls = _get_coordinator_class()
    coord = object.__new__(cls)
    coord.hass = _hass(states)
    coord.config = {
        "climate_entity": CLIMATE,
        "weather_entity": WEATHER,
        "indoor_temp_source": "climate_fallback",
        "sleep_indoor_temp_entity": BED_TEMP,
        "outdoor_temp_source": "sensor",
        "outdoor_temp_entity": OUT_TEMP,
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
    return coord


def _humidity(coord, *, sleep: bool, indoor=76.0, outdoor=92.0):
    with patch.object(_coord_mod(), "_in_sleep_window", return_value=sleep):
        return coord._read_chart_humidity(indoor_f=indoor, outdoor_f=outdoor)


class TestDayNightPairing:
    def test_provenance_is_sleep_window_swapped(self, registry, frozen_now):
        """The docstring's promise this feature relies on: in the sleep window the comfort reading is the bedroom."""
        frozen_now(NOW)
        coord = _coord(_house())
        mod = _coord_mod()
        with patch.object(mod, "_in_sleep_window", return_value=True):
            assert coord._get_indoor_temp_with_provenance().source_entity == BED_TEMP
        with patch.object(mod, "_in_sleep_window", return_value=False):
            assert coord._get_indoor_temp_with_provenance().source_entity == CLIMATE

    def test_day_row_reads_the_day_room(self, registry, frozen_now):
        frozen_now(NOW)
        out = _humidity(_coord(_house()), sleep=False, indoor=76.0)
        assert out["humidity"] == 52.4
        assert "feelslike" not in out  # 76 F / 52 %: below the NWS regression -> no indoor feels-like

    def test_sleep_row_reads_the_bedroom(self, registry, frozen_now):
        frozen_now(NOW)
        assert _humidity(_coord(_house()), sleep=True, indoor=71.2)["humidity"] == 61.7

    def test_sleep_row_without_bedroom_humidity_has_none(self, registry, frozen_now):
        """Never the hallway's 52.4 % or the thermostat's 41 % for the bedroom row."""
        frozen_now(NOW)
        out = _humidity(_coord(_house(**{BED_RH: None})), sleep=True, indoor=71.2)
        assert "humidity" not in out and "feelslike" not in out

    def test_user_choices_per_role(self, registry, frozen_now):
        frozen_now(NOW)
        st = _house(**{"sensor.day": _state("40.5"), "sensor.night": _state("44.5"), "sensor.out": _state("70.5")})
        coord = _coord(
            st,
            indoor_humidity_entity="sensor.day",
            sleep_indoor_humidity_entity="sensor.night",
            outdoor_humidity_entity="sensor.out",
        )
        assert _humidity(coord, sleep=False)["humidity"] == 40.5
        assert _humidity(coord, sleep=True)["humidity"] == 44.5
        assert _humidity(coord, sleep=False)["outdoor_humidity"] == 70.5

    def test_dedicated_indoor_sensor_device(self, registry, frozen_now):
        """indoor_temp_source = sensor: the comfort sensor's own device is searched after the thermostat's."""
        frozen_now(NOW)
        coord = _coord(_house(**{HALL_RH: None}), indoor_temp_source="sensor", indoor_temp_entity=HALL_TEMP)
        assert _humidity(coord, sleep=False)["humidity"] == 50.0


class TestFeelsLikeOnRows:
    def test_hot_humid_afternoon(self, registry, frozen_now):
        frozen_now(NOW)
        st = _house(**{HALL_RH: _state("70.0"), OUT_RH: _state("70.0")})
        out = _humidity(_coord(st), sleep=False, indoor=90.0, outdoor=90.0)
        assert out["feelslike"] == pytest.approx(105.922, abs=1e-3)
        assert out["outdoor_feelslike"] == pytest.approx(105.922, abs=1e-3)

    def test_mild_day_has_humidity_but_no_feels_like(self, registry, frozen_now):
        frozen_now(NOW)
        out = _humidity(_coord(_house()), sleep=False, indoor=72.0, outdoor=70.0)
        assert out["humidity"] == 52.4 and out["outdoor_humidity"] == 58.25
        assert "feelslike" not in out and "outdoor_feelslike" not in out

    @pytest.mark.parametrize(("speed", "unit"), [(20.0, "mph"), (32.18688, "km/h"), (8.9408, "m/s"), (17.37959, "kn")])
    def test_cold_windy_row_has_wind_chill(self, registry, frozen_now, speed, unit):
        """30 F in a 20 mph wind (any wind unit the weather entity reports) feels like 17.4 F; no wind chill indoors."""
        frozen_now(NOW)
        st = _house(**{WEATHER: _state("cloudy", {"humidity": 80.0, "wind_speed": speed, "wind_speed_unit": unit})})
        out = _humidity(_coord(st), sleep=False, indoor=45.0, outdoor=30.0)
        assert out["outdoor_feelslike"] == pytest.approx(17.3618, abs=1e-3)
        assert "feelslike" not in out

    def test_no_outdoor_on_the_row_means_no_outdoor_feels_like(self, registry, frozen_now):
        frozen_now(NOW)
        out = _humidity(_coord(_house()), sleep=False, indoor=76.0, outdoor=None)
        assert "outdoor_feelslike" not in out and out["outdoor_humidity"] == 58.25

    def test_row_context_carries_the_fields(self, registry, frozen_now):
        frozen_now(NOW)
        coord = _coord(_house(**{HALL_RH: _state("70.0")}))
        coord._read_chart_hold = lambda: {}
        coord._read_chart_windows_planned = lambda: {}
        with patch.object(_coord_mod(), "_in_sleep_window", return_value=False):
            ctx = coord._read_chart_context(indoor=90.0, outdoor=92.0)
        assert ctx["humidity"] == 70.0 and ctx["band_sensor"] == CLIMATE
        assert ctx["feelslike"] == pytest.approx(105.922, abs=1e-3)

    def test_a_fault_never_breaks_the_row(self, frozen_now):
        frozen_now(NOW)
        coord = _coord({})
        coord.hass.states.get = MagicMock(side_effect=RuntimeError("boom"))
        assert _humidity(coord, sleep=False) == {}

    def test_discovery_cached_per_entity_and_class(self, registry):
        from custom_components.climate_advisor.const import COMPANION_SENSOR_REFRESH_SECONDS

        coord = _coord(_house())
        mod = _coord_mod()
        with (
            patch.object(mod, "discover_companion_sensors", side_effect=lambda *a, **k: (HALL_RH,)) as disc,
            patch.object(mod, "_monotonic") as mono,
        ):
            mono.return_value = 1000.0
            coord._companion_sensors(CLIMATE, "humidity")
            coord._companion_sensors(CLIMATE, "humidity")
            coord._companion_sensors(BED_TEMP, "humidity")
            assert disc.call_count == 2
            mono.return_value = 1000.0 + COMPANION_SENSOR_REFRESH_SECONDS + 1
            coord._companion_sensors(CLIMATE, "humidity")
            assert disc.call_count == 3


# ── ChartStateLog: the fields, the buckets, the size ─────────────────────────────────────────────────────────────────
_T0 = datetime(2026, 7, 14, 14, 0, tzinfo=UTC)


def _chart_log(tmp_path, rows):
    mod = importlib.import_module("custom_components.climate_advisor.chart_log")
    with patch.object(mod.dt_util, "now", return_value=_T0 + timedelta(hours=1)):
        log = mod.ChartStateLog(tmp_path)
        for r in rows:
            log.append(hvac="cool", fan=False, indoor=76.0, outdoor=90.0, **r)
    return log


class TestChartLogFields:
    def test_written_only_when_not_none(self, tmp_path):
        log = _chart_log(
            tmp_path,
            [
                {"ts": _T0.isoformat(), "humidity": 52.437, "feelslike": None, "outdoor_humidity": 58.0},
                {"ts": (_T0 + timedelta(minutes=10)).isoformat()},
            ],
        )
        a, b = log._entries
        assert a["humidity"] == 52.437 and a["outdoor_humidity"] == 58.0  # native resolution, never rounded
        assert "feelslike" not in a and "outdoor_feelslike" not in a
        assert not ({"humidity", "feelslike", "outdoor_humidity", "outdoor_feelslike"} & set(b))

    @pytest.mark.parametrize("bucket", ["_bucket_hourly", "_bucket_daily"])
    def test_bucket_mean_over_rows_that_carry_the_key(self, tmp_path, bucket):
        log = _chart_log(
            tmp_path,
            [
                {"ts": _T0.isoformat(), "humidity": 50.0, "feelslike": 100.0},
                {"ts": (_T0 + timedelta(minutes=20)).isoformat(), "humidity": 55.0},
                {"ts": (_T0 + timedelta(minutes=40)).isoformat(), "humidity": 60.5},
            ],
        )
        [b] = getattr(log, bucket)(log._entries)
        assert b["humidity"] == pytest.approx(55.1666667)
        assert b["feelslike"] == 100.0  # only the row where NWS defined it, never averaged with absent rows
        assert "outdoor_humidity" not in b and "outdoor_feelslike" not in b

    def test_size_budget(self, tmp_path):
        """docs/chart-log-spec.md Retention Model: an ordinary row without humidity is unchanged; the worst case adds
        at most 140 bytes (4 keys, full float repr), about 5.1 MB a year per zone at the measured ~105 rows a day.
        A typical row (2 humidity keys at a sensor's 1-2 dp) adds about 45 bytes, about 1.6 MB a year."""
        plain = _chart_log(tmp_path, [{"ts": _T0.isoformat()}])
        full = _chart_log(
            tmp_path,
            [
                {
                    "ts": _T0.isoformat(),
                    "humidity": 52.43333333333333,
                    "feelslike": 105.92202060000027,
                    "outdoor_humidity": 58.25,
                    "outdoor_feelslike": -19.397954107996384,
                }
            ],
        )
        typical = _chart_log(tmp_path, [{"ts": _T0.isoformat(), "humidity": 52.4, "outdoor_humidity": 58.25}])

        def size(log) -> int:
            return len(json.dumps(log._entries[0], separators=(",", ":")))

        worst = size(full) - size(plain)
        assert 0 < worst <= 140
        assert worst * 105 * 365 < 5.2 * 1024 * 1024
        assert size(typical) - size(plain) <= 45


# ── get_chart_data(): °C conversion and the forecast lists ───────────────────────────────────────────────────────────
def _historical(tmp_path, temp_unit: str) -> dict:
    from tests.test_forecast_chart_integration import (
        _NOW,
        _daily_6day_forecast,
        _make_full_chart_coord,
        _patched_dt_util,
    )

    mod = importlib.import_module("custom_components.climate_advisor.chart_log")
    anchor = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    now_utc = _NOW.replace(tzinfo=UTC)
    with patch.object(mod.dt_util, "now", return_value=now_utc):
        log = mod.ChartStateLog(tmp_path)
        for i in range(4):
            log.append(
                hvac="cool",
                fan=False,
                indoor=90.0,
                outdoor=30.0,
                humidity=70.0,
                feelslike=104.0,
                outdoor_humidity=80.0,
                outdoor_feelslike=14.0,
                ts=(anchor - timedelta(hours=3) + timedelta(minutes=30 * i)).isoformat(),
            )
    coord = _make_full_chart_coord(daily_forecast_full=_daily_6day_forecast(), temp_unit=temp_unit)
    coord._chart_log = log
    now_p, as_local_p = _patched_dt_util(_coord_mod(), now=now_utc)
    with now_p, as_local_p:
        return {r: coord.get_chart_data(r, before_ts=anchor.timestamp())["state_log"] for r in ("24h", "7d", "1y")}


@pytest.mark.parametrize(("temp_unit", "fl", "ofl"), [("fahrenheit", 104.0, 14.0), ("celsius", 40.0, -10.0)])
def test_state_log_converts_both_feels_like_fields(tmp_path, temp_unit, fl, ofl):
    for range_str, rows in _historical(tmp_path, temp_unit).items():
        assert rows, range_str
        for e in rows:
            assert e["feelslike"] == pytest.approx(fl, abs=0.05), range_str
            assert e["outdoor_feelslike"] == pytest.approx(ofl, abs=0.05), range_str
            assert e["humidity"] == 70.0 and e["outdoor_humidity"] == 80.0  # % is never converted


def _live(forecast: list[dict], *, temp_unit="fahrenheit", wind_unit="mph", historical=False) -> dict:
    from tests.test_forecast_chart_integration import _NOW, _make_full_chart_coord, _patched_dt_util

    coord = _make_full_chart_coord(daily_forecast_full=None, temp_unit=temp_unit)
    coord.config["weather_entity"] = WEATHER
    coord.hass = _hass({WEATHER: _state("sunny", {"wind_speed_unit": wind_unit})})
    coord._hourly_forecast_temps = forecast
    now_p, as_local_p = _patched_dt_util(_coord_mod(), now=_NOW.replace(tzinfo=UTC) if historical else _NOW)
    with now_p, as_local_p:
        if historical:
            return coord.get_chart_data("24h", before_ts=(_NOW - timedelta(days=3)).replace(tzinfo=UTC).timestamp())
        return coord.get_chart_data("24h")


def _fc(hours: list[tuple[int, float, float | None, float | None]]) -> list[dict]:
    from tests.test_forecast_chart_integration import _NOW

    out = []
    for h, temp, rh, wind in hours:
        e = {"datetime": (_NOW + timedelta(hours=h)).isoformat(), "temperature": temp}
        if rh is not None:
            e["humidity"] = rh
        if wind is not None:
            e["wind_speed"] = wind
        out.append(e)
    return out


class TestForecastLists:
    def test_present_future_only_with_feels_like_rule(self):
        res = _live(_fc([(-1, 95.0, 70.0, 5.0), (0, 95.0, 70.0, 5.0), (1, 90.0, 70.0, 5.0), (2, 72.0, 50.0, 5.0)]))
        hum = res["outdoor_humidity_forecast"]
        assert [p["value"] for p in hum] == [70.0, 50.0]  # ts > now only
        fl = res["outdoor_feelslike_forecast"]
        assert len(fl) == 1 and fl[0]["value"] == pytest.approx(105.9, abs=0.05)  # mild hour omitted

    def test_wind_chill_point_uses_the_entity_wind_unit(self):
        res = _live(_fc([(1, 30.0, 80.0, 32.18688)]), wind_unit="km/h")
        assert res["outdoor_feelslike_forecast"][0]["value"] == pytest.approx(17.4, abs=0.05)

    def test_celsius_feels_like_forecast(self):
        res = _live(_fc([(1, 90.0, 70.0, None)]), temp_unit="celsius")
        assert res["outdoor_feelslike_forecast"][0]["value"] == pytest.approx((105.922 - 32) * 5 / 9, abs=0.06)
        assert res["outdoor_humidity_forecast"][0]["value"] == 70.0

    def test_absent_when_provider_has_no_humidity(self):
        res = _live(_fc([(1, 95.0, None, 5.0), (2, 96.0, None, 5.0)]))
        assert "outdoor_humidity_forecast" not in res and "outdoor_feelslike_forecast" not in res

    def test_absent_in_a_historical_view(self):
        res = _live(_fc([(1, 95.0, 70.0, 5.0)]), historical=True)
        assert "outdoor_humidity_forecast" not in res


# ── chart display only: nothing in automation / control reads any of it ──────────────────────────────────────────────
_NEW_NAMES = {
    "feels_like",
    "humidity_sources",
    "feels_like_f",
    "heat_index_f",
    "wind_chill_f",
    "resolve_humidity",
    "_read_chart_humidity",
    "_build_outdoor_humidity_feelslike_forecast",
    "CONF_INDOOR_HUMIDITY_ENTITY",
    "CONF_SLEEP_INDOOR_HUMIDITY_ENTITY",
    "CONF_OUTDOOR_HUMIDITY_ENTITY",
}
_NEW_STRINGS = {
    "feelslike",
    "outdoor_feelslike",
    "outdoor_humidity",
    "indoor_humidity_entity",
    "sleep_indoor_humidity_entity",
    "outdoor_humidity_entity",
}
# The only modules allowed to name the new code or keys: the two new modules, the chart log, the settings surfaces.
_ALLOWED_MODULES = {
    "feels_like.py",
    "humidity_sources.py",
    "chart_log.py",
    "coordinator.py",
    "const.py",
    "config_flow.py",
}


def _names_and_strings(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            found.add(n.id)
        elif isinstance(n, ast.Attribute):
            found.add(n.attr)
        elif isinstance(n, ast.alias):
            found.update(n.name.split("."))
        elif isinstance(n, ast.ImportFrom) and n.module:
            found.update(n.module.split("."))
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            found.add(n.value)
    return found


def test_no_automation_or_control_module_reads_the_new_values():
    offenders = {}
    for path in sorted(PKG.glob("*.py")):
        if path.name in _ALLOWED_MODULES:
            continue
        hits = _names_and_strings(ast.parse(path.read_text(encoding="utf-8"))) & (_NEW_NAMES | _NEW_STRINGS)
        if hits:
            offenders[path.name] = sorted(hits)
    assert not offenders, f"chart-only humidity / feels-like reached a non-chart module: {offenders}"


def test_in_the_coordinator_only_the_chart_paths_touch_it():
    src = (PKG / "coordinator.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ClimateAdvisorCoordinator")
    users = set()
    for fn in cls.body:
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef) and _names_and_strings(fn) & (
            _NEW_NAMES - {"feels_like", "humidity_sources"}
        ):
            users.add(fn.name)
    assert users == {"_read_chart_humidity", "_read_chart_context", "get_chart_data"}
    # and _read_chart_context's output only ever goes into a chart_log row
    readers = set()
    for fn in cls.body:
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            for n in ast.walk(fn):
                if isinstance(n, ast.Attribute) and n.attr == "_read_chart_context":
                    readers.add(fn.name)
    assert src.count("**self._read_chart_context(") == 5
    assert src.count("self._read_chart_context(") == 5
    assert readers and "_read_chart_context" not in readers
