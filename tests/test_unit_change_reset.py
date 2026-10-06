"""Tests for Issue #1015 — temperature-unit change reset and weather-unit mismatch.

Covers:
  - unit_key_from_attr() mapping table
  - Issue #968/#1018 climate-entity unit check (_check_climate_unit): errors when the
    thermostat's reported unit differs from the configured unit; latched once checkable
  - _build_state_dict() persists "temp_unit"
  - async_restore_state(): same-day readings (temp history, pred archive, classification)
    are discarded only when the persisted unit differs from the configured unit, in both
    directions; today_record/occupancy are kept; missing/non-str unit is a no-op
  - the restore WARNING and the "unit_changed" event
  - _get_forecast(): one-shot weather-provider unit mismatch WARNING + debug-state fields
  - _get_forecast(): observed-extreme override WARNING (> _OBSERVED_EXTREME_DELTA_F)
  - diagnostics chart_data_summary range/age keys (incl. empty history)
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import types
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.diagnostics import _chart_data_summary  # noqa: E402
from custom_components.climate_advisor.temperature import (  # noqa: E402
    CELSIUS,
    FAHRENHEIT,
    unit_key_from_attr,
)

_COORD_LOGGER = "custom_components.climate_advisor.coordinator"
_FIXED_NOW = datetime(2026, 10, 6, 12, 0, 0)
_TODAY_STR = _FIXED_NOW.strftime("%Y-%m-%d")


def _get_coordinator_class():
    """Fetch the live coordinator class (test_occupancy.py re-imports the module)."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _coord_module():
    return importlib.import_module("custom_components.climate_advisor.coordinator")


# ---------------------------------------------------------------------------
# unit_key_from_attr
# ---------------------------------------------------------------------------


class TestUnitKeyFromAttr:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("°F", FAHRENHEIT),
            ("°C", CELSIUS),
            ("F", FAHRENHEIT),
            ("C", CELSIUS),
            ("f", FAHRENHEIT),
            ("c", CELSIUS),
            ("fahrenheit", FAHRENHEIT),
            ("Celsius", CELSIUS),
            (" °c ", CELSIUS),
            ("K", None),
            ("kelvin", None),
            ("", None),
            (None, None),
        ],
    )
    def test_table(self, raw, expected):
        assert unit_key_from_attr(raw) == expected


# ---------------------------------------------------------------------------
# Existing Issue #968 climate-entity check (placement in the _first_run block)
# ---------------------------------------------------------------------------


def _make_climate_coord(configured: str, state, *, with_latch_attr: bool = True):
    """Stub coordinator exposing the real _check_climate_unit; ``state`` may be a list
    of successive states returned by hass.states.get (last one repeats)."""
    cls = _get_coordinator_class()
    coord = object.__new__(cls)
    coord.config = {"climate_entity": "climate.test", "temp_unit": configured}
    states = list(state) if isinstance(state, list) else [state]
    calls = {"n": 0}

    def _get(_eid):
        s = states[min(calls["n"], len(states) - 1)]
        calls["n"] += 1
        return s

    coord.hass = MagicMock()
    coord.hass.states.get = _get
    if with_latch_attr:
        coord._climate_unit_checked = False
    coord._check = types.MethodType(cls._check_climate_unit, coord)
    return coord


def _cstate(raw="__absent__", state="heat"):
    s = MagicMock()
    s.state = state
    s.attributes = {} if raw == "__absent__" else {"temperature_unit": raw}
    return s


def _climate_errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR and r.name == _COORD_LOGGER]


class TestClimateUnitCheck:
    """Real _check_climate_unit (Issues #968/#1018): thermostat unit must equal config."""

    @pytest.mark.parametrize(("configured", "raw"), [("celsius", "°C"), ("celsius", "C"), ("fahrenheit", "°F")])
    def test_matching_unit_silent(self, caplog, configured, raw):
        coord = _make_climate_coord(configured, _cstate(raw))
        with caplog.at_level(logging.ERROR, logger=_COORD_LOGGER):
            coord._check()
        assert _climate_errors(caplog) == []

    def test_absent_attribute_silent_and_latches(self, caplog):
        coord = _make_climate_coord("fahrenheit", _cstate())
        with caplog.at_level(logging.ERROR, logger=_COORD_LOGGER):
            coord._check()
        assert _climate_errors(caplog) == []
        assert coord._climate_unit_checked is True

    def test_celsius_config_fahrenheit_thermostat_errors(self, caplog):
        coord = _make_climate_coord("celsius", _cstate("°F"))
        with caplog.at_level(logging.ERROR, logger=_COORD_LOGGER):
            coord._check()
        msgs = _climate_errors(caplog)
        assert len(msgs) == 1
        assert "°F" in msgs[0]
        assert "celsius" in msgs[0]

    def test_fahrenheit_config_celsius_thermostat_errors(self, caplog):
        coord = _make_climate_coord("fahrenheit", _cstate("°C"))
        with caplog.at_level(logging.ERROR, logger=_COORD_LOGGER):
            coord._check()
        msgs = _climate_errors(caplog)
        assert len(msgs) == 1
        assert "°C" in msgs[0]
        assert "fahrenheit" in msgs[0]

    def test_unrecognised_unit_errors(self, caplog):
        coord = _make_climate_coord("fahrenheit", _cstate("K"))
        with caplog.at_level(logging.ERROR, logger=_COORD_LOGGER):
            coord._check()
        assert len(_climate_errors(caplog)) == 1

    def test_missing_entity_does_not_latch_then_checks_when_available(self, caplog):
        coord = _make_climate_coord("celsius", [None, _cstate("°F")])
        with caplog.at_level(logging.ERROR, logger=_COORD_LOGGER):
            coord._check()
            assert coord._climate_unit_checked is False
            assert _climate_errors(caplog) == []
            coord._check()
        assert coord._climate_unit_checked is True
        assert len(_climate_errors(caplog)) == 1

    @pytest.mark.parametrize("st", ["unavailable", "unknown"])
    def test_unavailable_state_does_not_latch(self, caplog, st):
        coord = _make_climate_coord("celsius", [_cstate("°F", state=st), _cstate("°F")])
        with caplog.at_level(logging.ERROR, logger=_COORD_LOGGER):
            coord._check()
            assert coord._climate_unit_checked is False
            assert _climate_errors(caplog) == []
            coord._check()
        assert len(_climate_errors(caplog)) == 1

    def test_latched_second_call_silent(self, caplog):
        coord = _make_climate_coord("celsius", _cstate("°F"))
        with caplog.at_level(logging.ERROR, logger=_COORD_LOGGER):
            coord._check()
            coord._check()
        assert len(_climate_errors(caplog)) == 1

    def test_stub_without_latch_attribute_works(self, caplog):
        coord = _make_climate_coord("celsius", _cstate("°F"), with_latch_attr=False)
        assert not hasattr(coord, "_climate_unit_checked")
        with caplog.at_level(logging.ERROR, logger=_COORD_LOGGER):
            coord._check()
        assert len(_climate_errors(caplog)) == 1


# ---------------------------------------------------------------------------
# Save / restore
# ---------------------------------------------------------------------------


def _make_dt_mock():
    dt_mock = MagicMock()
    dt_mock.now.return_value = _FIXED_NOW
    dt_mock.parse_datetime.side_effect = lambda s: datetime.fromisoformat(s) if s else None
    return dt_mock


def _make_coordinator(unit: str):
    """Coordinator stub wired for both _build_state_dict() and async_restore_state()."""
    cls = _get_coordinator_class()
    coord = object.__new__(cls)
    coord.config = {"temp_unit": unit}
    learning = MagicMock()
    learning._state = MagicMock()
    learning._state.rejection_log = {}
    learning._state.last_shutdown_version = None
    learning._state.clean_shutdown = True
    coord.learning = learning
    coord._state_persistence = MagicMock()
    coord._chart_log = MagicMock()
    coord._event_log = []
    coord._rejection_log = {}
    coord._current_classification = None
    coord._today_record = None
    coord._outdoor_temp_history = []
    coord._indoor_temp_history = []
    coord._briefing_sent_today = False
    coord._last_briefing = ""
    coord._last_briefing_short = ""
    coord._briefing_day_type = None
    coord._automation_enabled = True
    coord._occupancy_mode = "home"
    coord._occupancy_away_since = None
    coord.claude_client = None
    coord._pred_archive = {}
    coord._passive_k_backfilled = False
    coord._vent_window_k_backfilled = False
    coord._vent_fan_k_backfilled = False
    coord._passive_k_backfill_v2 = False
    coord._vent_window_k_backfill_v2 = False
    coord._vent_fan_k_backfill_v2 = False
    coord._solar_phase_backfill = False
    coord._solar_phase_ac_backfill = False
    coord._last_solar_phase_fit_date = None
    ae = MagicMock()
    ae.get_serializable_state = MagicMock(return_value={})
    ae.restore_state = MagicMock()
    ae.set_occupancy_mode = MagicMock()
    ae.dry_run = False
    coord.automation_engine = ae
    coord._build_state_dict = types.MethodType(cls._build_state_dict, coord)
    coord._emit_event = types.MethodType(cls._emit_event, coord)
    coord._get_indoor_temp = MagicMock(return_value=72.0)  # read by _emit_event when config is set
    coord.async_restore_state = types.MethodType(cls.async_restore_state, coord)
    return coord


def _saved_state(unit_key_present: bool = True, unit=FAHRENHEIT) -> dict:
    state = {
        "date": _TODAY_STR,
        "last_saved": _FIXED_NOW.isoformat(),
        "classification": {
            "day_type": "warm",
            "trend_direction": "stable",
            "trend_magnitude": 0,
            "today_high": 80.0,
            "today_low": 59.5,
            "tomorrow_high": 78.0,
            "tomorrow_low": 58.0,
        },
        "temp_history": {
            "outdoor": [["2026-10-06T08:00:00", 61.0], ["2026-10-06T09:00:00", 63.0]],
            "indoor": [["2026-10-06T08:00:00", 71.0]],
        },
        "pred_archive": {"8": 70.5, "9": 71.0},
        "today_record": {"date": _TODAY_STR, "day_type": "warm", "trend_direction": "stable"},
        "occupancy_mode": "away",
        "event_log": [{"type": "earlier", "time": _FIXED_NOW.isoformat()}],
    }
    if unit_key_present:
        state["temp_unit"] = unit
    return state


def _run_restore(coord, state: dict) -> None:
    async def _fake_executor(fn, *args):
        if getattr(fn, "__wrapped__", fn) is coord._state_persistence.load:
            return state
        return None

    coord.hass = MagicMock()
    coord.hass.async_add_executor_job = _fake_executor
    with patch(f"{_COORD_LOGGER}.dt_util", _make_dt_mock()):
        asyncio.run(coord.async_restore_state())


class TestSaveIncludesUnit:
    @pytest.mark.parametrize("unit", [FAHRENHEIT, CELSIUS])
    def test_build_state_dict_has_temp_unit(self, unit):
        coord = _make_coordinator(unit)
        with patch(f"{_COORD_LOGGER}.dt_util", _make_dt_mock()):
            assert coord._build_state_dict()["temp_unit"] == unit


class TestRestoreUnitChange:
    def test_same_unit_keeps_everything(self):
        coord = _make_coordinator(FAHRENHEIT)
        _run_restore(coord, _saved_state(unit=FAHRENHEIT))
        assert len(coord._outdoor_temp_history) == 2
        assert len(coord._indoor_temp_history) == 1
        assert coord._pred_archive == {8: 70.5, 9: 71.0}
        assert coord._current_classification is not None
        assert not any(e["type"] == "unit_changed" for e in coord._event_log)

    @pytest.mark.parametrize(
        ("saved", "current"),
        [(FAHRENHEIT, CELSIUS), (CELSIUS, FAHRENHEIT)],
    )
    def test_changed_unit_drops_readings_keeps_rest(self, saved, current, caplog):
        coord = _make_coordinator(current)
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            _run_restore(coord, _saved_state(unit=saved))
        assert coord._outdoor_temp_history == []
        assert coord._indoor_temp_history == []
        assert coord._pred_archive == {}
        assert coord._current_classification is None
        # Kept
        assert coord._today_record is not None
        assert coord._today_record.day_type == "warm"
        assert coord._occupancy_mode == "away"
        assert any(e["type"] == "earlier" for e in coord._event_log)
        # Warning content
        msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        warn = next(m for m in msgs if "Temperature unit changed since last run" in m)
        assert f"from={saved}" in warn
        assert f"to={current}" in warn
        assert "dropped_outdoor=2" in warn
        assert "dropped_indoor=1" in warn
        assert "dropped_pred_archive=2" in warn
        assert "dropped_classification=True" in warn
        # Event content (survives the event-log restore)
        evt = next(e for e in coord._event_log if e["type"] == "unit_changed")
        assert evt["from"] == saved
        assert evt["to"] == current
        assert evt["dropped_outdoor"] == 2
        assert evt["dropped_indoor"] == 1
        assert evt["dropped_pred_archive"] == 2
        assert evt["dropped_classification"] is True
        assert evt["dropped_pending_observations"] == 0

    @pytest.mark.parametrize(("saved", "current", "cleared"), [(FAHRENHEIT, CELSIUS, True), (CELSIUS, CELSIUS, False)])
    def test_pending_observations_cleared_only_on_change(self, saved, current, cleared, caplog):
        coord = _make_coordinator(current)
        pending = {"passive_decay": {"samples": [1, 2]}, "hvac_heat": {"samples": [3]}}
        coord.learning._state.pending_observations = pending
        coord.learning._state.rejection_log = {"keep": "me"}
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            _run_restore(coord, _saved_state(unit=saved))
        if cleared:
            assert coord.learning._state.pending_observations == {}
            assert isinstance(coord.learning._state.pending_observations, dict)
            evt = next(e for e in coord._event_log if e["type"] == "unit_changed")
            assert evt["dropped_pending_observations"] == 2
            assert any("dropped_pending_observations=2" in r.getMessage() for r in caplog.records)
        else:
            assert set(coord.learning._state.pending_observations) == {"passive_decay", "hvac_heat"}
        # Other learning data is never touched
        assert coord.learning._state.rejection_log == {"keep": "me"}

    def test_forecast_after_changed_unit_restore_uses_provider_values(self):
        """A poisoned same-day history (16.7 stored while unit mismatched) must not
        survive a unit-change restore into the next real _get_forecast()."""
        poisoned = _saved_state(unit=CELSIUS)
        poisoned["temp_history"]["outdoor"] = [["2026-05-15T05:00:00", 60.0], ["2026-05-15T05:30:00", 16.7]]
        fc = [_entry(_TODAY, 80.0, 59.5)]

        changed = _make_coordinator(FAHRENHEIT)
        _run_restore(changed, poisoned)
        fc_coord = _make_forecast_coord(
            unit=FAHRENHEIT,
            attrs={"temperature": 60.0, "temperature_unit": "°F"},
            history=changed._outdoor_temp_history,
            forecast=fc,
        )
        assert _run_get_forecast(fc_coord).today_low == pytest.approx(59.5)

        # Control: same unit keeps the history, so the poisoned low does win.
        same = _make_coordinator(FAHRENHEIT)
        _run_restore(same, {**poisoned, "temp_unit": FAHRENHEIT})
        fc_coord2 = _make_forecast_coord(
            unit=FAHRENHEIT,
            attrs={"temperature": 60.0, "temperature_unit": "°F"},
            history=same._outdoor_temp_history,
            forecast=fc,
        )
        assert _run_get_forecast(fc_coord2).today_low == pytest.approx(16.7)

    def test_missing_unit_is_noop(self, caplog):
        coord = _make_coordinator(CELSIUS)
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            _run_restore(coord, _saved_state(unit_key_present=False))
        assert len(coord._outdoor_temp_history) == 2
        assert coord._current_classification is not None
        assert not any("unit changed" in r.getMessage() for r in caplog.records)
        assert not any(e["type"] == "unit_changed" for e in coord._event_log)

    @pytest.mark.parametrize("bad", [None, 5, ["celsius"], {"u": 1}])
    def test_non_str_unit_is_noop(self, bad):
        coord = _make_coordinator(CELSIUS)
        _run_restore(coord, _saved_state(unit=bad))
        assert len(coord._outdoor_temp_history) == 2
        assert coord._pred_archive == {8: 70.5, 9: 71.0}
        assert not any(e["type"] == "unit_changed" for e in coord._event_log)

    def test_stub_without_config_is_noop(self):
        coord = _make_coordinator(CELSIUS)
        del coord.config
        _run_restore(coord, _saved_state(unit=FAHRENHEIT))
        assert len(coord._outdoor_temp_history) == 2


# ---------------------------------------------------------------------------
# _get_forecast: weather-unit mismatch + observed-extreme override
# ---------------------------------------------------------------------------

_PDT = timezone(-timedelta(hours=7))
_NOW_LOCAL = datetime(2026, 5, 15, 6, 0, 0, tzinfo=_PDT)
_TODAY = date(2026, 5, 15)


def _make_forecast_coord(*, unit: str, attrs: dict, outdoor_f: float = 65.0, history=None, forecast=None):
    cls = _get_coordinator_class()
    coord = MagicMock()
    coord.hass = MagicMock()
    weather_state = MagicMock()
    weather_state.state = "sunny"
    weather_state.attributes = attrs
    coord.hass.states.get = MagicMock(return_value=weather_state)
    coord.config = {
        "climate_entity": "climate.test",
        "weather_entity": "weather.test",
        "temp_unit": unit,
        "learning_enabled": False,
    }
    coord._outdoor_temp_history = list(history or [])
    coord._weather_unit_checked = False
    coord._weather_unit_info = None
    coord._get_forecast = types.MethodType(cls._get_forecast, coord)
    coord._get_forecast_data = AsyncMock(return_value=forecast or [])
    coord._get_outdoor_temp = MagicMock(return_value=outdoor_f)
    coord._get_indoor_temp = MagicMock(return_value=72.0)
    coord.learning = MagicMock()
    coord.learning.get_weather_bias = MagicMock(return_value={"confidence": "none"})
    return coord


def _run_get_forecast(coord):
    async def run():
        with patch(f"{_COORD_LOGGER}.dt_util.now", return_value=_NOW_LOCAL):
            return await coord._get_forecast()

    return asyncio.run(run())


def _mismatch_msgs(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "Weather unit mismatch" in r.getMessage()]


class TestWeatherUnitMismatch:
    @pytest.mark.parametrize(
        ("config_unit", "provider_attr", "provider_key"),
        [(CELSIUS, "°F", FAHRENHEIT), (FAHRENHEIT, "°C", CELSIUS)],
    )
    def test_fires_once_and_latches(self, config_unit, provider_attr, provider_key, caplog):
        coord = _make_forecast_coord(unit=config_unit, attrs={"temperature": 18.0, "temperature_unit": provider_attr})
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            _run_get_forecast(coord)
            _run_get_forecast(coord)
        msgs = _mismatch_msgs(caplog)
        assert len(msgs) == 1
        assert f"provider_unit={provider_key}" in msgs[0]
        assert f"configured_unit={config_unit}" in msgs[0]
        assert coord._weather_unit_checked is True
        assert coord._weather_unit_info == {
            "provider_unit": provider_key,
            "configured_unit": config_unit,
            "unit_mismatch": True,
        }

    @pytest.mark.parametrize(("config_unit", "attr"), [(CELSIUS, "°C"), (FAHRENHEIT, "°F")])
    def test_silent_when_matching(self, config_unit, attr, caplog):
        coord = _make_forecast_coord(unit=config_unit, attrs={"temperature": 18.0, "temperature_unit": attr})
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            _run_get_forecast(coord)
        assert _mismatch_msgs(caplog) == []
        assert coord._weather_unit_checked is True
        assert coord._weather_unit_info["unit_mismatch"] is False

    def test_silent_and_unlatched_when_attribute_absent(self, caplog):
        coord = _make_forecast_coord(unit=CELSIUS, attrs={"temperature": 18.0})
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            _run_get_forecast(coord)
        assert _mismatch_msgs(caplog) == []
        assert coord._weather_unit_checked is False
        assert coord._weather_unit_info is None

    def test_silent_when_already_checked(self, caplog):
        coord = _make_forecast_coord(unit=CELSIUS, attrs={"temperature": 65.0, "temperature_unit": "°F"})
        coord._weather_unit_checked = True
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            _run_get_forecast(coord)
        assert _mismatch_msgs(caplog) == []

    def test_message_has_no_entity_id(self, caplog):
        coord = _make_forecast_coord(unit=CELSIUS, attrs={"temperature": 65.0, "temperature_unit": "°F"})
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            _run_get_forecast(coord)
        assert "weather.test" not in _mismatch_msgs(caplog)[0]

    def _debug_state(self, unit_info):
        cls = _get_coordinator_class()
        coord = object.__new__(cls)
        coord.config = {"temp_unit": FAHRENHEIT}
        coord._weather_unit_info = unit_info
        coord._resolved_sensors = []
        coord._door_open_timers = {}
        coord._current_classification = None
        coord._automation_enabled = True
        coord._occupancy_mode = "home"
        coord._occupancy_away_timer_cancel = None
        coord._startup_coalesce_active = False
        coord._startup_coalesce_expiry = None
        coord.data = None
        ae = MagicMock()
        ae.config = {}
        ae._manual_override_active = False
        ae._grace_active = False
        ae._natural_vent_active = False
        ae._fan_active = False
        ae._decision_lock_held_since = None
        coord.automation_engine = ae
        # Collaborators that read unrelated subsystems (not under test)
        coord.compute_nat_vent_cycling_band = lambda: {
            "nat_vent_target": None,
            "nat_vent_on_threshold": None,
            "nat_vent_off_threshold": None,
        }
        coord._compute_fan_remote_status_fields = lambda: {
            "fan_remote_timer_hours": None,
            "fan_remote_timer_ends": None,
            "fan_remote_speed": None,
        }
        coord.get_thermostat_fan_only_runtime_today = lambda: 0.0
        coord._build_thermal_pipeline_summary = lambda: {}
        return types.MethodType(cls.get_debug_state, coord)()

    def test_debug_state_reports_mismatch(self):
        state = self._debug_state(
            {"provider_unit": CELSIUS, "configured_unit": FAHRENHEIT, "unit_mismatch": True},
        )
        assert state["unit_mismatch"] is True
        assert state["provider_unit"] == CELSIUS
        assert state["configured_unit"] == FAHRENHEIT

    def test_debug_state_none_safe_before_check(self):
        state = self._debug_state(None)
        assert state["unit_mismatch"] is None
        assert state["provider_unit"] is None
        assert state["configured_unit"] is None


def _entry(d: date, high: float, low: float) -> dict:
    return {"datetime": f"{d.isoformat()}T12:00:00-07:00", "temperature": high, "templow": low}


class TestObservedExtremeOverride:
    def _warnings(self, caplog) -> list[str]:
        return [
            r.getMessage() for r in caplog.records if "Observed outdoor extreme overrides forecast" in r.getMessage()
        ]

    def test_low_override_warns_with_ts_and_values(self, caplog):
        # Provider low 59.5F; a stray unit-confused sample of 16.7 drags the observed low.
        history = [("2026-05-15T05:00:00", 60.0), ("2026-05-15T05:30:00", 16.7)]
        coord = _make_forecast_coord(
            unit=FAHRENHEIT,
            attrs={"temperature": 60.0, "temperature_unit": "°F"},
            history=history,
            forecast=[_entry(_TODAY, 80.0, 59.5)],
        )
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            result = _run_get_forecast(coord)
        msgs = self._warnings(caplog)
        assert len(msgs) == 1
        assert "kind=low" in msgs[0]
        assert "observed_ts=2026-05-15T05:30:00" in msgs[0]
        assert "observed_value=16.7" in msgs[0]
        assert "provider_value=59.5" in msgs[0]
        # Behaviour unchanged: observed low still wins
        assert result.today_low == pytest.approx(16.7)

    def test_high_override_warns(self, caplog):
        history = [("2026-05-15T05:00:00", 60.0), ("2026-05-15T05:30:00", 120.0)]
        coord = _make_forecast_coord(
            unit=FAHRENHEIT,
            attrs={"temperature": 60.0, "temperature_unit": "°F"},
            history=history,
            forecast=[_entry(_TODAY, 80.0, 59.5)],
        )
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            result = _run_get_forecast(coord)
        msgs = self._warnings(caplog)
        assert len(msgs) == 1
        assert "kind=high" in msgs[0]
        assert "observed_ts=2026-05-15T05:30:00" in msgs[0]
        assert result.today_high == pytest.approx(120.0)

    def test_repeated_cycles_warn_once_then_new_extreme_warns_again(self, caplog):
        history = [("2026-05-15T05:00:00", 60.0), ("2026-05-15T05:30:00", 16.7)]
        coord = _make_forecast_coord(
            unit=FAHRENHEIT,
            attrs={"temperature": 60.0, "temperature_unit": "°F"},
            history=history,
            forecast=[_entry(_TODAY, 80.0, 59.5)],
        )
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            _run_get_forecast(coord)
            _run_get_forecast(coord)
            _run_get_forecast(coord)
            assert len(self._warnings(caplog)) == 1
            # A new, lower extreme sample is a distinct (kind, ts) => warns again
            coord._outdoor_temp_history.append(("2026-05-15T06:00:00", 10.0))
            _run_get_forecast(coord)
            _run_get_forecast(coord)
        msgs = self._warnings(caplog)
        assert len(msgs) == 2
        assert "observed_ts=2026-05-15T06:00:00" in msgs[1]

    def test_latch_cleared_at_end_of_day(self):
        """_async_end_of_day() resets the latch next to the history buffers it clears."""
        import inspect

        src = inspect.getsource(_get_coordinator_class()._async_end_of_day)
        assert "self._observed_extreme_warned = set()" in src

    def test_normal_day_silent(self, caplog):
        history = [("2026-05-15T05:00:00", 55.0), ("2026-05-15T05:30:00", 84.0)]
        coord = _make_forecast_coord(
            unit=FAHRENHEIT,
            attrs={"temperature": 60.0, "temperature_unit": "°F"},
            history=history,
            forecast=[_entry(_TODAY, 80.0, 59.5)],
        )
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            _run_get_forecast(coord)
        assert self._warnings(caplog) == []

    def test_large_fahrenheit_swing_inside_forecast_silent(self, caplog):
        # Observed extremes inside the provider's own range never override => silent.
        history = [("2026-05-15T05:00:00", 45.0), ("2026-05-15T05:30:00", 90.0)]
        coord = _make_forecast_coord(
            unit=FAHRENHEIT,
            attrs={"temperature": 60.0, "temperature_unit": "°F"},
            history=history,
            forecast=[_entry(_TODAY, 95.0, 40.0)],
        )
        with caplog.at_level(logging.WARNING, logger=_COORD_LOGGER):
            _run_get_forecast(coord)
        assert self._warnings(caplog) == []


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


class TestDiagnosticsChartSummary:
    def test_populated(self):
        coord = MagicMock()
        coord._outdoor_temp_history = [("t1", 61.0), ("t2", 16.7), ("t3", 63.0)]
        coord._indoor_temp_history = [("t1", 71.0)]
        assert _chart_data_summary(coord) == {
            "outdoor_points": 3,
            "indoor_points": 1,
            "outdoor_min": 16.7,
            "outdoor_max": 63.0,
            "outdoor_oldest_ts": "t1",
            "outdoor_newest_ts": "t3",
        }

    def test_empty(self):
        coord = MagicMock()
        coord._outdoor_temp_history = []
        coord._indoor_temp_history = []
        assert _chart_data_summary(coord) == {
            "outdoor_points": 0,
            "indoor_points": 0,
            "outdoor_min": None,
            "outdoor_max": None,
            "outdoor_oldest_ts": None,
            "outdoor_newest_ts": None,
        }
