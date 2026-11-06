"""Issue #1230 R0: every chart_log row records EVERY configured indoor sensor (``indoor_sensors``).

Occupant-first: the v4 thermal model fits one line per indoor sensor (hallway thermostat, bedroom sensor, ...), so a
row must carry what each sensor read even while CA acts on only one (the sleep window swaps the comfort value to the
bedroom sensor). The field is informational: nothing reads it for a decision or a display.
"""

from __future__ import annotations

import ast
import datetime as _dt
import inspect
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import pytest  # noqa: E402

from custom_components.climate_advisor.chart_log import ChartStateLog  # noqa: E402
from custom_components.climate_advisor.const import INDOOR_SENSOR_STALE_HOURS  # noqa: E402
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402
from custom_components.climate_advisor.indoor_temp import read_indoor_sensors_f  # noqa: E402

NOW = _dt.datetime(2026, 11, 6, 12, 0, tzinfo=_dt.UTC)
CLIMATE = "climate.hallway"
SENSOR = "sensor.hallway_temp"
SLEEP = "sensor.bedroom_temp"


def _state(value, attributes: dict | None = None, age_h: float | None = None) -> MagicMock:
    s = MagicMock()
    s.state = value
    s.attributes = attributes or {}
    if age_h is not None:
        s.last_reported = NOW - _dt.timedelta(hours=age_h)
        s.last_updated = NOW - _dt.timedelta(hours=age_h)
    return s


def _hass(states: dict) -> MagicMock:
    hass = MagicMock()
    hass.states.get = lambda eid: states.get(eid)
    return hass


def _read(states, *, unit="fahrenheit", sensor=SENSOR, sleep=SLEEP, climate=CLIMATE):
    return read_indoor_sensors_f(
        hass=_hass(states),
        unit=unit,
        climate_entity=climate,
        indoor_temp_entity=sensor,
        sleep_indoor_temp_entity=sleep,
        now=NOW,
    )


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


class TestReader:
    def test_all_sources_configured(self):
        out = _read(
            {
                CLIMATE: _state("heat", {"current_temperature": 71.46}),
                SENSOR: _state("70.18"),
                SLEEP: _state("66.004"),
            }
        )
        assert out == {CLIMATE: 71.46, SENSOR: 70.18, SLEEP: 66.0}
        assert list(out) == [CLIMATE, SENSOR, SLEEP]

    def test_only_climate_configured(self):
        out = _read({CLIMATE: _state("heat", {"current_temperature": 70})}, sensor=None, sleep=None)
        assert out == {CLIMATE: 70.0}

    def test_no_climate_entity_configured(self):
        assert _read({SENSOR: _state("70")}, climate=None, sleep=None) == {SENSOR: 70.0}

    def test_two_decimal_resolution_kept(self):
        assert _read({SENSOR: _state("70.184")}, climate=None, sleep=None) == {SENSOR: 70.18}

    def test_sleep_sensor_unavailable_is_null_others_unaffected(self):
        out = _read(
            {CLIMATE: _state("heat", {"current_temperature": 70}), SENSOR: _state("69"), SLEEP: _state("unavailable")}
        )
        assert out == {CLIMATE: 70.0, SENSOR: 69.0, SLEEP: None}

    def test_missing_entity_is_null(self):
        assert _read({CLIMATE: _state("heat", {"current_temperature": 70})}) == {
            CLIMATE: 70.0,
            SENSOR: None,
            SLEEP: None,
        }

    def test_non_numeric_nan_implausible_are_null(self):
        out = _read(
            {CLIMATE: _state("heat", {"current_temperature": 150}), SENSOR: _state("warm"), SLEEP: _state("nan")}
        )
        assert out == {CLIMATE: None, SENSOR: None, SLEEP: None}

    def test_stale_sensor_is_null_but_input_number_exempt(self):
        old = INDOOR_SENSOR_STALE_HOURS + 1
        out = _read(
            {SENSOR: _state("70", age_h=old), "input_number.bed": _state("65", age_h=500)},
            climate=None,
            sleep="input_number.bed",
        )
        assert out == {SENSOR: None, "input_number.bed": 65.0}

    def test_fresh_sensor_within_threshold(self):
        out = _read({SENSOR: _state("70", age_h=INDOOR_SENSOR_STALE_HOURS - 1)}, climate=None, sleep=None)
        assert out == {SENSOR: 70.0}

    def test_celsius_install_stored_in_fahrenheit(self):
        out = _read(
            {CLIMATE: _state("heat", {"current_temperature": 21}), SENSOR: _state("20"), SLEEP: _state("18.5")},
            unit="celsius",
        )
        assert out[CLIMATE] == pytest.approx(69.8, abs=0.01)
        assert out[SENSOR] == pytest.approx(68.0, abs=0.01)
        assert out[SLEEP] == pytest.approx(65.3, abs=0.01)

    def test_celsius_implausible_after_conversion_is_null(self):
        assert _read({SENSOR: _state("60")}, unit="celsius", climate=None, sleep=None) == {SENSOR: None}

    def test_duplicate_entity_listed_once(self):
        out = _read({SENSOR: _state("70")}, climate=None, sensor=SENSOR, sleep=SENSOR)
        assert out == {SENSOR: 70.0}

    def test_never_raises_and_a_bad_source_does_not_block_the_rest(self):
        hass = MagicMock()

        def _get(eid):
            if eid == SENSOR:
                raise RuntimeError("boom")
            return _state("66") if eid == SLEEP else None

        hass.states.get = _get
        out = read_indoor_sensors_f(
            hass=hass,
            unit="fahrenheit",
            climate_entity=None,
            indoor_temp_entity=SENSOR,
            sleep_indoor_temp_entity=SLEEP,
            now=NOW,
        )
        assert out == {SENSOR: None, SLEEP: 66.0}

    def test_quiet_above_debug_and_no_events(self, caplog):
        with caplog.at_level(logging.DEBUG):
            _read({CLIMATE: _state("unavailable"), SENSOR: _state("warm"), SLEEP: _state("150")})
        assert all(r.levelno <= logging.DEBUG for r in caplog.records)


# ---------------------------------------------------------------------------
# Coordinator wrapper: sleep window on/off give identical snapshots
# ---------------------------------------------------------------------------


def _coord(states: dict, **cfg) -> ClimateAdvisorCoordinator:
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = _hass(states)
    coord.config = {
        "climate_entity": CLIMATE,
        "indoor_temp_source": "climate_fallback",
        "indoor_temp_entity": SENSOR,
        "sleep_indoor_temp_entity": SLEEP,
        "temp_unit": "fahrenheit",
        **cfg,
    }
    return coord


class TestCoordinatorSnapshot:
    STATES = {
        CLIMATE: _state("heat", {"current_temperature": 71.5}),
        SENSOR: _state("70.18"),
        SLEEP: _state("66.2"),
    }

    def test_sleep_window_does_not_change_the_snapshot(self, frozen_now):
        frozen_now(NOW)
        coord = _coord(self.STATES)
        with patch("custom_components.climate_advisor.coordinator._in_sleep_window", return_value=False):
            day = coord._read_chart_indoor_sensors()
            day_comfort = coord._get_indoor_temp_with_provenance().value
        with patch("custom_components.climate_advisor.coordinator._in_sleep_window", return_value=True):
            night = coord._read_chart_indoor_sensors()
            night_comfort = coord._get_indoor_temp_with_provenance().value
        assert day == night == {CLIMATE: 71.5, SENSOR: 70.18, SLEEP: 66.2}
        assert (day_comfort, night_comfort) == (71.5, 66.2)  # the comfort value DID swap; the snapshot did not

    def test_source_in_use_does_not_change_the_snapshot(self, frozen_now):
        frozen_now(NOW)
        a = _coord(self.STATES, indoor_temp_source="climate_fallback")._read_chart_indoor_sensors()
        b = _coord(self.STATES, indoor_temp_source="sensor")._read_chart_indoor_sensors()
        assert a == b

    def test_wrapper_never_raises(self):
        coord = object.__new__(ClimateAdvisorCoordinator)  # no hass/config at all
        assert coord._read_chart_indoor_sensors() == {}

    def test_every_append_call_site_writes_indoor_sensors(self):
        """Structural: a new self._chart_log.append(...) call that forgets the field fails here."""
        import custom_components.climate_advisor.coordinator as coord_mod

        tree = ast.parse(Path(inspect.getsourcefile(coord_mod)).read_text(encoding="utf-8"))
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "append"
            and isinstance(n.func.value, ast.Attribute)
            and n.func.value.attr == "_chart_log"
        ]
        assert len(calls) >= 4
        for call in calls:
            kw = {k.arg: k.value for k in call.keywords}
            assert "indoor_sensors" in kw, f"chart_log.append at coordinator.py:{call.lineno} lacks indoor_sensors"
            assert ast.unparse(kw["indoor_sensors"]) == "self._read_chart_indoor_sensors()"


# ---------------------------------------------------------------------------
# ChartStateLog: append, load of legacy rows, bucketing
# ---------------------------------------------------------------------------


def _ts(h: int, m: int = 0, day: int = 6) -> str:
    return datetime(2026, 11, day, h, m, tzinfo=UTC).isoformat()


class TestChartLog:
    def test_append_with_field(self, tmp_path):
        log = ChartStateLog(tmp_path)
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=None, indoor_sensors={CLIMATE: 70.0, SLEEP: None})
        assert log._entries[0]["indoor_sensors"] == {CLIMATE: 70.0, SLEEP: None}

    def test_empty_mapping_is_kept_none_omits(self, tmp_path):
        log = ChartStateLog(tmp_path)
        log.append(hvac="off", fan=False, indoor=None, outdoor=None, indoor_sensors={})
        log.append(hvac="off", fan=False, indoor=None, outdoor=None)
        assert log._entries[0]["indoor_sensors"] == {}
        assert "indoor_sensors" not in log._entries[1]

    def test_append_copies_the_mapping(self, tmp_path):
        log = ChartStateLog(tmp_path)
        src = {CLIMATE: 70.0}
        log.append(hvac="off", fan=False, indoor=None, outdoor=None, indoor_sensors=src)
        src[CLIMATE] = 99.0
        assert log._entries[0]["indoor_sensors"] == {CLIMATE: 70.0}

    def test_roundtrip_and_legacy_rows_load(self, tmp_path):
        legacy = {"ts": datetime.now(UTC).isoformat(), "hvac": "off", "fan": False, "indoor": 70.0, "outdoor": 50.0}
        (tmp_path / "climate_advisor_chart_log.json").write_text(json.dumps({"entries": [legacy]}))
        log = ChartStateLog(tmp_path)
        log.load()
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=50.0, indoor_sensors={CLIMATE: 70.0})
        log.save()
        again = ChartStateLog(tmp_path)
        again.load()
        assert "indoor_sensors" not in again._entries[0]
        assert again._entries[1]["indoor_sensors"] == {CLIMATE: 70.0}
        assert again.get_entries("24h") is not None  # legacy + new rows both query fine

    def test_hourly_bucket_per_entity_mean(self, tmp_path):
        log = ChartStateLog(tmp_path)
        log.append(
            hvac="off", fan=False, indoor=70, outdoor=50, ts=_ts(10, 0), indoor_sensors={CLIMATE: 70.0, SLEEP: 66.0}
        )
        log.append(
            hvac="off", fan=False, indoor=70, outdoor=50, ts=_ts(10, 30), indoor_sensors={CLIMATE: 70.5, SLEEP: None}
        )
        log.append(hvac="off", fan=False, indoor=70, outdoor=50, ts=_ts(10, 45))  # legacy-shaped row
        (b,) = log._bucket_hourly(log._entries)
        assert b["indoor_sensors"] == {CLIMATE: 70.25, SLEEP: 66.0}  # None readings are skipped, not averaged as 0

    def test_hourly_bucket_all_null_entity_stays_null(self, tmp_path):
        log = ChartStateLog(tmp_path)
        log.append(hvac="off", fan=False, indoor=None, outdoor=None, ts=_ts(10), indoor_sensors={SLEEP: None})
        (b,) = log._bucket_hourly(log._entries)
        assert b["indoor_sensors"] == {SLEEP: None}

    def test_bucket_without_any_field_omits_key(self, tmp_path):
        log = ChartStateLog(tmp_path)
        log.append(hvac="off", fan=False, indoor=70, outdoor=50, ts=_ts(10))
        assert "indoor_sensors" not in log._bucket_hourly(log._entries)[0]
        assert "indoor_sensors" not in log._bucket_daily(log._entries)[0]

    def test_daily_bucket_per_entity_mean(self, tmp_path):
        log = ChartStateLog(tmp_path)
        log.append(hvac="off", fan=False, indoor=70, outdoor=50, ts=_ts(1), indoor_sensors={CLIMATE: 68.0})
        log.append(hvac="off", fan=False, indoor=70, outdoor=50, ts=_ts(13), indoor_sensors={CLIMATE: 72.0})
        log.append(hvac="off", fan=False, indoor=70, outdoor=50, ts=_ts(13, day=7), indoor_sensors={CLIMATE: 60.0})
        a, b = log._bucket_daily(log._entries)
        assert a["indoor_sensors"] == {CLIMATE: 70.0}
        assert b["indoor_sensors"] == {CLIMATE: 60.0}


# ---------------------------------------------------------------------------
# Issue #1230 R1: companion temperature sensors on the climate entity's own device
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from custom_components.climate_advisor.indoor_temp import discover_companion_temp_sensors  # noqa: E402

COMPANION = "sensor.hallway_current_temperature"


def _reg_entry(entity_id, *, device_id="dev1", device_class=None, original_device_class=None):
    return SimpleNamespace(
        entity_id=entity_id,
        domain=entity_id.split(".")[0],
        device_id=device_id,
        device_class=device_class,
        original_device_class=original_device_class,
    )


def _fake_er(entries: dict, *, disabled: set[str] = frozenset()):
    """A stand-in for the ``entity_registry`` module: ``entries`` maps entity_id -> registry entry."""
    reg = SimpleNamespace(async_get=lambda eid: entries.get(eid))

    def siblings(_reg, device_id, include_disabled_entities=True):
        assert include_disabled_entities is False  # the contract: disabled entities are never logged
        return [e for e in entries.values() if e.device_id == device_id and e.entity_id not in disabled]

    return SimpleNamespace(async_get=lambda _hass: reg, async_entries_for_device=siblings)


def _discover(entries, **kw):
    with patch("custom_components.climate_advisor.indoor_temp.er", _fake_er(entries, **kw)):
        return discover_companion_temp_sensors(MagicMock(), CLIMATE)


class TestCompanionDiscovery:
    def test_temperature_sensor_on_same_device_found(self):
        entries = {
            CLIMATE: _reg_entry(CLIMATE),
            COMPANION: _reg_entry(COMPANION, original_device_class="temperature"),
        }
        assert _discover(entries) == (COMPANION,)

    def test_non_temperature_companions_excluded(self):
        entries = {
            CLIMATE: _reg_entry(CLIMATE),
            "sensor.hallway_humidity": _reg_entry("sensor.hallway_humidity", original_device_class="humidity"),
            "binary_sensor.hallway_motion": _reg_entry("binary_sensor.hallway_motion", original_device_class="motion"),
            "sensor.hallway_misc": _reg_entry("sensor.hallway_misc"),
            "sensor.other_device_temp": _reg_entry(
                "sensor.other_device_temp", device_id="dev2", original_device_class="temperature"
            ),
        }
        assert _discover(entries) == ()

    def test_user_device_class_override_wins(self):
        entries = {
            CLIMATE: _reg_entry(CLIMATE),
            COMPANION: _reg_entry(COMPANION, device_class="temperature", original_device_class="humidity"),
            "sensor.x": _reg_entry("sensor.x", device_class="humidity", original_device_class="temperature"),
        }
        assert _discover(entries) == (COMPANION,)

    def test_disabled_entity_excluded(self):
        entries = {
            CLIMATE: _reg_entry(CLIMATE),
            COMPANION: _reg_entry(COMPANION, original_device_class="temperature"),
        }
        assert _discover(entries, disabled={COMPANION}) == ()

    def test_no_device_or_not_in_registry_or_no_climate(self):
        assert _discover({CLIMATE: _reg_entry(CLIMATE, device_id=None)}) == ()
        assert _discover({}) == ()
        with patch("custom_components.climate_advisor.indoor_temp.er", _fake_er({})):
            assert discover_companion_temp_sensors(MagicMock(), None) == ()

    def test_registry_failure_never_raises(self):
        boom = SimpleNamespace(async_get=MagicMock(side_effect=RuntimeError("x")))
        with patch("custom_components.climate_advisor.indoor_temp.er", boom):
            assert discover_companion_temp_sensors(MagicMock(), CLIMATE) == ()

    def test_sorted_deterministic(self):
        a, b = "sensor.a_temp", "sensor.b_temp"
        entries = {
            CLIMATE: _reg_entry(CLIMATE),
            b: _reg_entry(b, original_device_class="temperature"),
            a: _reg_entry(a, original_device_class="temperature"),
        }
        assert _discover(entries) == (a, b)


class TestCompanionReading:
    def _read_c(self, states, companions=(COMPANION,), **kw):
        return read_indoor_sensors_f(
            hass=_hass(states),
            unit=kw.pop("unit", "fahrenheit"),
            climate_entity=CLIMATE,
            indoor_temp_entity=kw.pop("sensor", None),
            sleep_indoor_temp_entity=None,
            companion_entities=companions,
            now=NOW,
        )

    def test_companion_logged_at_native_resolution_after_configured(self):
        out = self._read_c({CLIMATE: _state("heat", {"current_temperature": 69}), COMPANION: _state("68.54")})
        assert out == {CLIMATE: 69, COMPANION: 68.54}
        assert list(out) == [CLIMATE, COMPANION]

    def test_companion_equal_to_configured_entity_deduped(self):
        states = {CLIMATE: _state("heat", {"current_temperature": 69}), COMPANION: _state("68.54")}
        out = self._read_c(states, sensor=COMPANION)
        assert list(out) == [CLIMATE, COMPANION] and out[COMPANION] == 68.54

    def test_no_companions_means_configured_sources_only(self):
        out = self._read_c({CLIMATE: _state("heat", {"current_temperature": 69})}, companions=())
        assert out == {CLIMATE: 69}

    def test_unavailable_companion_is_null(self):
        out = self._read_c({CLIMATE: _state("heat", {"current_temperature": 69}), COMPANION: _state("unavailable")})
        assert out == {CLIMATE: 69, COMPANION: None}

    def test_missing_companion_state_is_null(self):
        assert self._read_c({CLIMATE: _state("heat", {"current_temperature": 69})})[COMPANION] is None

    def test_stale_and_implausible_companion_are_null(self):
        assert self._read_c({COMPANION: _state("68.5", age_h=INDOOR_SENSOR_STALE_HOURS + 1)})[COMPANION] is None
        assert self._read_c({COMPANION: _state("12.0")})[COMPANION] is None

    def test_celsius_companion_stored_fahrenheit(self):
        out = self._read_c(
            {CLIMATE: _state("heat", {"current_temperature": 20.5}), COMPANION: _state("20.3")}, unit="celsius"
        )
        assert out[COMPANION] == pytest.approx(68.54, abs=0.005)
        assert out[CLIMATE] == pytest.approx(68.9, abs=0.005)


class TestCoordinatorCompanions:
    def test_coordinator_logs_companion_and_caches_discovery(self, frozen_now):
        frozen_now(NOW)
        coord = _coord(
            {
                CLIMATE: _state("heat", {"current_temperature": 69}),
                SENSOR: _state("70.18"),
                SLEEP: _state("66.2"),
                COMPANION: _state("68.54"),
            }
        )
        coord._companion_temp_sensors_cache = None
        with patch(
            "custom_components.climate_advisor.coordinator.discover_companion_temp_sensors", return_value=(COMPANION,)
        ) as disc:
            first = coord._read_chart_indoor_sensors()
            second = coord._read_chart_indoor_sensors()
        assert first == second == {CLIMATE: 69, SENSOR: 70.18, SLEEP: 66.2, COMPANION: 68.54}
        assert disc.call_count == 1  # cached: not a registry scan per row

    def test_discovery_refreshes_after_ttl(self):
        from custom_components.climate_advisor.const import COMPANION_SENSOR_REFRESH_SECONDS

        coord = _coord({})
        coord._companion_temp_sensors_cache = None
        with (
            patch(
                "custom_components.climate_advisor.coordinator.discover_companion_temp_sensors",
                side_effect=[(COMPANION,), ()],
            ) as disc,
            patch("custom_components.climate_advisor.coordinator._monotonic") as mono,
        ):
            mono.return_value = 1000.0
            assert coord._companion_temp_sensors() == (COMPANION,)
            mono.return_value = 1000.0 + COMPANION_SENSOR_REFRESH_SECONDS - 1
            assert coord._companion_temp_sensors() == (COMPANION,)
            mono.return_value = 1000.0 + COMPANION_SENSOR_REFRESH_SECONDS + 1
            assert coord._companion_temp_sensors() == ()
        assert disc.call_count == 2

    def test_state_log_still_strips_field_with_companions(self):
        from custom_components.climate_advisor.coordinator import _CHART_LOG_STATE_LOG_STRIPPED_KEYS

        assert "indoor_sensors" in _CHART_LOG_STATE_LOG_STRIPPED_KEYS
