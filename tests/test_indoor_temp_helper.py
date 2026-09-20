"""Tests for Issue #796 Step 10 — indoor temp read/validate dedup.

``AutomationEngine._get_indoor_temp_f()`` (automation.py) and
``ClimateAdvisorCoordinator._get_indoor_temp()`` (coordinator.py) previously
re-implemented the same source-resolution logic independently and had drifted:
the coordinator rejected physically implausible readings via a plausible-range
guard ([40, 110] °F); automation.py had no such guard on either source path, and
its climate_fallback path had no exception handling around the numeric
conversion (a non-numeric ``current_temperature`` would raise uncaught).

Both now delegate to the shared ``indoor_temp.resolve_indoor_temp_f()`` helper.
These tests exercise the plausibility guard and non-numeric handling through
BOTH call paths (the real bound AutomationEngine method and the real bound
coordinator method) to confirm they now behave identically, plus the shared
helper directly for the source-type matrix (sensor/input_number vs
climate_fallback).
"""

from __future__ import annotations

import datetime as _dt
import sys
import types
from unittest.mock import MagicMock, patch

# ── HA module stubs (must happen before importing climate_advisor) ──────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.automation import AutomationEngine  # noqa: E402
from custom_components.climate_advisor.const import (  # noqa: E402
    TEMP_SOURCE_CLIMATE_FALLBACK,
    TEMP_SOURCE_SENSOR,
)
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402
from custom_components.climate_advisor.indoor_temp import (  # noqa: E402
    MAX_PLAUSIBLE_INDOOR_F,
    MIN_PLAUSIBLE_INDOOR_F,
    resolve_indoor_temp_f,
    resolve_indoor_temp_with_provenance,
)


def _make_state(state_value, attributes: dict | None = None) -> MagicMock:
    mock = MagicMock()
    mock.state = state_value
    mock.attributes = attributes or {}
    return mock


def _make_engine(*, config_overrides: dict | None = None, states_get_return=None) -> AutomationEngine:
    """Build a minimal AutomationEngine stub with the real _get_indoor_temp_f bound."""
    hass = MagicMock()
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=states_get_return)

    ae = object.__new__(AutomationEngine)
    ae.hass = hass
    ae.climate_entity = "climate.thermostat"
    ae.config = {
        "climate_entity": "climate.thermostat",
        "indoor_temp_source": TEMP_SOURCE_CLIMATE_FALLBACK,
        "temp_unit": "fahrenheit",
    }
    if config_overrides:
        ae.config.update(config_overrides)
    ae._get_indoor_temp_f = types.MethodType(AutomationEngine._get_indoor_temp_f, ae)
    return ae


def _make_coord(*, config_overrides: dict | None = None, states_get_return=None) -> ClimateAdvisorCoordinator:
    """Build a minimal coordinator stub with the real _get_indoor_temp bound."""
    hass = MagicMock()
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=states_get_return)

    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = hass
    coord.config = {
        "climate_entity": "climate.thermostat",
        "indoor_temp_source": TEMP_SOURCE_CLIMATE_FALLBACK,
        "temp_unit": "fahrenheit",
    }
    if config_overrides:
        coord.config.update(config_overrides)
    coord._get_indoor_temp = types.MethodType(ClimateAdvisorCoordinator._get_indoor_temp, coord)
    return coord


# ---------------------------------------------------------------------------
# Plausibility guard applies identically through both call paths
# ---------------------------------------------------------------------------


class TestPlausibilityGuardBothPaths:
    """The [40, 110] °F guard must reject the same values via either class."""

    def test_engine_rejects_extreme_low_climate_fallback(self):
        state = _make_state("heat", {"current_temperature": 25})
        ae = _make_engine(states_get_return=state)
        assert ae._get_indoor_temp_f() is None

    def test_coord_rejects_extreme_low_climate_fallback(self):
        state = _make_state("heat", {"current_temperature": 25})
        coord = _make_coord(states_get_return=state)
        assert coord._get_indoor_temp() is None

    def test_engine_rejects_extreme_high_climate_fallback(self):
        state = _make_state("heat", {"current_temperature": 120})
        ae = _make_engine(states_get_return=state)
        assert ae._get_indoor_temp_f() is None

    def test_coord_rejects_extreme_high_climate_fallback(self):
        state = _make_state("heat", {"current_temperature": 120})
        coord = _make_coord(states_get_return=state)
        assert coord._get_indoor_temp() is None

    def test_engine_accepts_normal_climate_fallback(self):
        state = _make_state("heat", {"current_temperature": 72})
        ae = _make_engine(states_get_return=state)
        assert ae._get_indoor_temp_f() == 72.0

    def test_coord_accepts_normal_climate_fallback(self):
        state = _make_state("heat", {"current_temperature": 72})
        coord = _make_coord(states_get_return=state)
        assert coord._get_indoor_temp() == 72.0

    def test_engine_rejects_extreme_low_sensor_source(self):
        state = _make_state("25")
        ae = _make_engine(
            config_overrides={
                "indoor_temp_source": TEMP_SOURCE_SENSOR,
                "indoor_temp_entity": "sensor.indoor_temp",
            },
            states_get_return=state,
        )
        assert ae._get_indoor_temp_f() is None

    def test_coord_rejects_extreme_low_sensor_source(self):
        state = _make_state("25")
        coord = _make_coord(
            config_overrides={
                "indoor_temp_source": TEMP_SOURCE_SENSOR,
                "indoor_temp_entity": "sensor.indoor_temp",
            },
            states_get_return=state,
        )
        assert coord._get_indoor_temp() is None

    def test_engine_accepts_normal_sensor_source(self):
        state = _make_state("72")
        ae = _make_engine(
            config_overrides={
                "indoor_temp_source": TEMP_SOURCE_SENSOR,
                "indoor_temp_entity": "sensor.indoor_temp",
            },
            states_get_return=state,
        )
        assert ae._get_indoor_temp_f() == 72.0

    def test_coord_accepts_normal_sensor_source(self):
        state = _make_state("72")
        coord = _make_coord(
            config_overrides={
                "indoor_temp_source": TEMP_SOURCE_SENSOR,
                "indoor_temp_entity": "sensor.indoor_temp",
            },
            states_get_return=state,
        )
        assert coord._get_indoor_temp() == 72.0


# ---------------------------------------------------------------------------
# Non-numeric handling — previously a real behavioral gap in automation.py
# ---------------------------------------------------------------------------


class TestNonNumericHandledBothPaths:
    """A non-numeric current_temperature must be treated as unavailable, not raise.

    Before this fix, AutomationEngine._get_indoor_temp_f()'s climate_fallback
    path had no try/except around float(temp) and would raise ValueError
    uncaught. The shared helper now catches it on both call paths.
    """

    def test_engine_climate_fallback_non_numeric_returns_none_not_raises(self):
        state = _make_state("heat", {"current_temperature": "unavailable"})
        ae = _make_engine(states_get_return=state)
        assert ae._get_indoor_temp_f() is None

    def test_coord_climate_fallback_non_numeric_returns_none_not_raises(self):
        state = _make_state("heat", {"current_temperature": "unavailable"})
        coord = _make_coord(states_get_return=state)
        assert coord._get_indoor_temp() is None

    def test_engine_sensor_source_non_numeric_returns_none(self):
        state = _make_state("unavailable")
        ae = _make_engine(
            config_overrides={
                "indoor_temp_source": TEMP_SOURCE_SENSOR,
                "indoor_temp_entity": "sensor.indoor_temp",
            },
            states_get_return=state,
        )
        assert ae._get_indoor_temp_f() is None

    def test_coord_sensor_source_non_numeric_returns_none(self):
        state = _make_state("unavailable")
        coord = _make_coord(
            config_overrides={
                "indoor_temp_source": TEMP_SOURCE_SENSOR,
                "indoor_temp_entity": "sensor.indoor_temp",
            },
            states_get_return=state,
        )
        assert coord._get_indoor_temp() is None


# ---------------------------------------------------------------------------
# Shared helper — direct source-type matrix
# ---------------------------------------------------------------------------


class TestResolveIndoorTempFDirect:
    """Direct tests of the shared helper across both source types."""

    def test_sensor_source_celsius_conversion(self):
        hass = MagicMock()
        hass.states.get.return_value = _make_state("20")
        result = resolve_indoor_temp_f(
            hass=hass,
            source=TEMP_SOURCE_SENSOR,
            unit="celsius",
            indoor_temp_entity="sensor.indoor_temp",
            climate_entity="climate.thermostat",
        )
        assert result is not None
        assert abs(result - 68.0) < 0.01

    def test_climate_fallback_celsius_conversion(self):
        hass = MagicMock()
        hass.states.get.return_value = _make_state("heat", {"current_temperature": 22})
        result = resolve_indoor_temp_f(
            hass=hass,
            source=TEMP_SOURCE_CLIMATE_FALLBACK,
            unit="celsius",
            indoor_temp_entity=None,
            climate_entity="climate.thermostat",
        )
        assert result is not None
        assert abs(result - 71.6) < 0.01

    def test_sensor_source_no_entity_configured_returns_none(self):
        hass = MagicMock()
        result = resolve_indoor_temp_f(
            hass=hass,
            source=TEMP_SOURCE_SENSOR,
            unit="fahrenheit",
            indoor_temp_entity=None,
            climate_entity="climate.thermostat",
        )
        assert result is None

    def test_sensor_source_state_missing_returns_none(self):
        hass = MagicMock()
        hass.states.get.return_value = None
        result = resolve_indoor_temp_f(
            hass=hass,
            source=TEMP_SOURCE_SENSOR,
            unit="fahrenheit",
            indoor_temp_entity="sensor.indoor_temp",
            climate_entity="climate.thermostat",
        )
        assert result is None

    def test_climate_fallback_state_missing_returns_none(self):
        hass = MagicMock()
        hass.states.get.return_value = None
        result = resolve_indoor_temp_f(
            hass=hass,
            source=TEMP_SOURCE_CLIMATE_FALLBACK,
            unit="fahrenheit",
            indoor_temp_entity=None,
            climate_entity="climate.thermostat",
        )
        assert result is None

    def test_climate_fallback_current_temperature_missing_returns_none(self):
        hass = MagicMock()
        hass.states.get.return_value = _make_state("heat", {})
        result = resolve_indoor_temp_f(
            hass=hass,
            source=TEMP_SOURCE_CLIMATE_FALLBACK,
            unit="fahrenheit",
            indoor_temp_entity=None,
            climate_entity="climate.thermostat",
        )
        assert result is None

    def test_boundary_values_are_inclusive(self):
        """MIN/MAX_PLAUSIBLE_INDOOR_F themselves are accepted (inclusive bounds)."""
        hass = MagicMock()
        hass.states.get.return_value = _make_state("heat", {"current_temperature": MIN_PLAUSIBLE_INDOOR_F})
        assert (
            resolve_indoor_temp_f(
                hass=hass,
                source=TEMP_SOURCE_CLIMATE_FALLBACK,
                unit="fahrenheit",
                indoor_temp_entity=None,
                climate_entity="climate.thermostat",
            )
            == MIN_PLAUSIBLE_INDOOR_F
        )

        hass.states.get.return_value = _make_state("heat", {"current_temperature": MAX_PLAUSIBLE_INDOOR_F})
        assert (
            resolve_indoor_temp_f(
                hass=hass,
                source=TEMP_SOURCE_CLIMATE_FALLBACK,
                unit="fahrenheit",
                indoor_temp_entity=None,
                climate_entity="climate.thermostat",
            )
            == MAX_PLAUSIBLE_INDOOR_F
        )


# ---------------------------------------------------------------------------
# Issue #895 — sleep-window bedroom sensor override
# ---------------------------------------------------------------------------


def _hass_with_entities(entity_states: dict) -> MagicMock:
    """A hass whose states.get() returns different states per entity_id."""
    hass = MagicMock()
    hass.states.get = MagicMock(side_effect=lambda eid: entity_states.get(eid))
    return hass


class TestSleepSensorOverrideDirect:
    """Each of the five distinct branches in resolve_indoor_temp_with_provenance()'s
    sleep-sensor path gets its own case — deliberately not folded into one combined
    test, since each exercises a different failure mode."""

    def test_valid_sleep_reading_used(self):
        hass = _hass_with_entities(
            {
                "climate.thermostat": _make_state("heat", {"current_temperature": 70}),
                "sensor.bedroom_temp": _make_state("65"),
            }
        )
        result = resolve_indoor_temp_with_provenance(
            hass=hass,
            source=TEMP_SOURCE_CLIMATE_FALLBACK,
            unit="fahrenheit",
            indoor_temp_entity=None,
            climate_entity="climate.thermostat",
            in_sleep_window=True,
            sleep_indoor_temp_entity="sensor.bedroom_temp",
        )
        assert result.value == 65.0
        assert result.source_entity == "sensor.bedroom_temp"
        assert result.primary_value == 70.0

    def test_sleep_entity_state_missing_falls_back(self):
        hass = _hass_with_entities({"climate.thermostat": _make_state("heat", {"current_temperature": 70})})
        result = resolve_indoor_temp_with_provenance(
            hass=hass,
            source=TEMP_SOURCE_CLIMATE_FALLBACK,
            unit="fahrenheit",
            indoor_temp_entity=None,
            climate_entity="climate.thermostat",
            in_sleep_window=True,
            sleep_indoor_temp_entity="sensor.bedroom_temp",
        )
        assert result.value == 70.0
        assert result.source_entity == "climate.thermostat"
        assert result.primary_value == 70.0

    def test_sleep_entity_non_numeric_falls_back(self):
        hass = _hass_with_entities(
            {
                "climate.thermostat": _make_state("heat", {"current_temperature": 70}),
                "sensor.bedroom_temp": _make_state("unavailable"),
            }
        )
        result = resolve_indoor_temp_with_provenance(
            hass=hass,
            source=TEMP_SOURCE_CLIMATE_FALLBACK,
            unit="fahrenheit",
            indoor_temp_entity=None,
            climate_entity="climate.thermostat",
            in_sleep_window=True,
            sleep_indoor_temp_entity="sensor.bedroom_temp",
        )
        assert result.value == 70.0
        assert result.primary_value == 70.0

    def test_sleep_entity_implausible_falls_back(self):
        hass = _hass_with_entities(
            {
                "climate.thermostat": _make_state("heat", {"current_temperature": 70}),
                "sensor.bedroom_temp": _make_state("200"),
            }
        )
        result = resolve_indoor_temp_with_provenance(
            hass=hass,
            source=TEMP_SOURCE_CLIMATE_FALLBACK,
            unit="fahrenheit",
            indoor_temp_entity=None,
            climate_entity="climate.thermostat",
            in_sleep_window=True,
            sleep_indoor_temp_entity="sensor.bedroom_temp",
        )
        assert result.value == 70.0
        assert result.primary_value == 70.0

    def test_both_sensors_unavailable_returns_none_cleanly(self):
        hass = _hass_with_entities({})
        result = resolve_indoor_temp_with_provenance(
            hass=hass,
            source=TEMP_SOURCE_CLIMATE_FALLBACK,
            unit="fahrenheit",
            indoor_temp_entity=None,
            climate_entity="climate.thermostat",
            in_sleep_window=True,
            sleep_indoor_temp_entity="sensor.bedroom_temp",
        )
        assert result == (None, None, None)

    def test_sleep_window_not_active_uses_primary_regardless_of_config(self):
        hass = _hass_with_entities(
            {
                "climate.thermostat": _make_state("heat", {"current_temperature": 70}),
                "sensor.bedroom_temp": _make_state("65"),
            }
        )
        result = resolve_indoor_temp_with_provenance(
            hass=hass,
            source=TEMP_SOURCE_CLIMATE_FALLBACK,
            unit="fahrenheit",
            indoor_temp_entity=None,
            climate_entity="climate.thermostat",
            in_sleep_window=False,
            sleep_indoor_temp_entity="sensor.bedroom_temp",
        )
        assert result.value == 70.0
        assert result.source_entity == "climate.thermostat"

    def test_sleep_entity_unset_uses_primary_even_in_window(self):
        hass = _hass_with_entities({"climate.thermostat": _make_state("heat", {"current_temperature": 70})})
        result = resolve_indoor_temp_with_provenance(
            hass=hass,
            source=TEMP_SOURCE_CLIMATE_FALLBACK,
            unit="fahrenheit",
            indoor_temp_entity=None,
            climate_entity="climate.thermostat",
            in_sleep_window=True,
            sleep_indoor_temp_entity=None,
        )
        assert result.value == 70.0

    def test_sleep_sensor_celsius_conversion(self):
        hass = _hass_with_entities(
            {
                "climate.thermostat": _make_state("heat", {"current_temperature": 22}),
                "sensor.bedroom_temp": _make_state("18"),
            }
        )
        result = resolve_indoor_temp_with_provenance(
            hass=hass,
            source=TEMP_SOURCE_CLIMATE_FALLBACK,
            unit="celsius",
            indoor_temp_entity=None,
            climate_entity="climate.thermostat",
            in_sleep_window=True,
            sleep_indoor_temp_entity="sensor.bedroom_temp",
        )
        assert result.value is not None
        assert abs(result.value - 64.4) < 0.01  # 18C -> 64.4F
        assert result.primary_value is not None
        assert abs(result.primary_value - 71.6) < 0.01  # 22C -> 71.6F


class TestSleepSensorMissSeverityEscalation:
    """Issue #949: resolve_indoor_temp_with_provenance()'s sleep-sensor severity model.

    Before the fix, every unavailable cycle logged a WARNING (no dedup, no streak
    tracking), so a one-cycle blip (HA restart) and a genuine hours-long outage were
    indistinguishable in the log, and nothing ever marked recovery. The fix tracks a
    per-entity consecutive-miss streak (module-level ``_sleep_sensor_miss_streaks``
    dict in indoor_temp.py): misses 1-2 log INFO, miss 3+ escalates to WARNING with
    the retry count, and recovery logs at the severity the outage actually reached
    (INFO if it never escalated, WARNING if it did) — the recovery line is the piece
    that was completely invisible before this fix for anything that reached WARNING.

    ``_sleep_sensor_miss_streaks`` is process-lifetime module state keyed by
    entity_id, not scoped per test — every test here must reset the streak for its
    own entity_id before running (and uses a dedicated entity_id, distinct from the
    one other test classes in this file reuse) so cross-test pollution can't produce
    a false pass/fail.
    """

    ENTITY_ID = "sensor.issue_949_sleep_severity_test"

    def setup_method(self, _method):
        # Reset this test's entity's streak before every test — module-level dict
        # persists across tests in the same process (see class docstring).
        from custom_components.climate_advisor import indoor_temp as _indoor_temp_mod

        _indoor_temp_mod._sleep_sensor_miss_streaks.pop(self.ENTITY_ID, None)

    def _resolve(self, hass):
        return resolve_indoor_temp_with_provenance(
            hass=hass,
            source=TEMP_SOURCE_CLIMATE_FALLBACK,
            unit="fahrenheit",
            indoor_temp_entity=None,
            climate_entity="climate.thermostat",
            in_sleep_window=True,
            sleep_indoor_temp_entity=self.ENTITY_ID,
        )

    def _unavailable_hass(self):
        return _hass_with_entities({"climate.thermostat": _make_state("heat", {"current_temperature": 70})})

    def _available_hass(self, sleep_value="65"):
        return _hass_with_entities(
            {
                "climate.thermostat": _make_state("heat", {"current_temperature": 70}),
                self.ENTITY_ID: _make_state(sleep_value),
            }
        )

    def test_misses_one_and_two_log_info_not_warning(self, caplog):
        import logging

        with caplog.at_level(logging.DEBUG, logger="custom_components.climate_advisor.indoor_temp"):
            self._resolve(self._unavailable_hass())  # miss 1
            self._resolve(self._unavailable_hass())  # miss 2

        assert len(caplog.records) == 2
        assert all(r.levelno == logging.INFO for r in caplog.records), (
            f"Expected both of the first two misses to log at INFO, got levels: {[r.levelname for r in caplog.records]}"
        )
        assert "unavailable (1/2)" in caplog.records[0].message
        assert "unavailable (2/2)" in caplog.records[1].message

    def test_third_consecutive_miss_escalates_to_warning_with_retry_count(self, caplog):
        import logging

        with caplog.at_level(logging.DEBUG, logger="custom_components.climate_advisor.indoor_temp"):
            self._resolve(self._unavailable_hass())  # miss 1 — INFO
            self._resolve(self._unavailable_hass())  # miss 2 — INFO
            self._resolve(self._unavailable_hass())  # miss 3 — escalates to WARNING

        assert caplog.records[-1].levelno == logging.WARNING
        assert "3 consecutive checks" in caplog.records[-1].message

    def test_recovery_after_never_escalating_logs_info(self, caplog):
        """Recovery after only 1-2 misses (never reached WARNING) must log the
        recovery itself at INFO — matching the severity the outage actually reached."""
        import logging

        with caplog.at_level(logging.DEBUG, logger="custom_components.climate_advisor.indoor_temp"):
            self._resolve(self._unavailable_hass())  # miss 1 — INFO
            caplog.clear()
            self._resolve(self._available_hass())  # recovery

        recovery_records = [r for r in caplog.records if "available again" in r.message]
        assert len(recovery_records) == 1, f"Expected exactly one recovery line, got: {caplog.records}"
        assert recovery_records[0].levelno == logging.INFO
        assert "1 consecutive miss" in recovery_records[0].message

    def test_recovery_after_escalation_logs_warning(self, caplog):
        """Recovery after crossing the WARNING threshold must log the recovery at
        WARNING too — this is the line that was completely invisible before Issue
        #949's fix (a WARNING-worthy outage recovering with zero trace in the log)."""
        import logging

        with caplog.at_level(logging.DEBUG, logger="custom_components.climate_advisor.indoor_temp"):
            self._resolve(self._unavailable_hass())  # miss 1 — INFO
            self._resolve(self._unavailable_hass())  # miss 2 — INFO
            self._resolve(self._unavailable_hass())  # miss 3 — WARNING (escalated)
            caplog.clear()
            self._resolve(self._available_hass())  # recovery

        recovery_records = [r for r in caplog.records if "available again" in r.message]
        assert len(recovery_records) == 1, f"Expected exactly one recovery line, got: {caplog.records}"
        assert recovery_records[0].levelno == logging.WARNING
        assert "3 consecutive misses" in recovery_records[0].message

    def test_streak_resets_after_recovery_so_a_later_outage_restarts_at_one(self):
        """A fresh outage after a full recovery must restart the streak at 1, not
        continue accumulating from the prior (resolved) outage."""
        from custom_components.climate_advisor import indoor_temp as _indoor_temp_mod

        self._resolve(self._unavailable_hass())  # miss 1
        self._resolve(self._unavailable_hass())  # miss 2
        self._resolve(self._unavailable_hass())  # miss 3 — escalated
        self._resolve(self._available_hass())  # recovery — streak resets to 0
        assert _indoor_temp_mod._sleep_sensor_miss_streaks[self.ENTITY_ID] == 0

        self._resolve(self._unavailable_hass())  # a new outage's miss 1
        assert _indoor_temp_mod._sleep_sensor_miss_streaks[self.ENTITY_ID] == 1

    def test_healthy_cycle_success_line_unaffected(self):
        """The existing per-cycle 'Using sleep indoor sensor...' INFO line on a
        healthy cycle (no prior miss) must be unchanged by this fix."""
        result = self._resolve(self._available_hass())
        assert result.value == 65.0
        assert result.source_entity == self.ENTITY_ID


class TestSleepSensorOverrideBothCallPaths:
    """The sleep-window swap must behave identically through both real bound methods,
    with deterministic 'now' so the sleep-window check doesn't depend on wall-clock
    time (per this project's existing dt_util-mocking convention)."""

    def _sleep_config(self) -> dict:
        return {
            "climate_entity": "climate.thermostat",
            "indoor_temp_source": TEMP_SOURCE_CLIMATE_FALLBACK,
            "temp_unit": "fahrenheit",
            "sleep_time": "22:00",
            "wake_time": "07:00",
            "sleep_indoor_temp_entity": "sensor.bedroom_temp",
        }

    def test_engine_uses_sleep_sensor_in_window(self):
        hass = _hass_with_entities(
            {
                "climate.thermostat": _make_state("heat", {"current_temperature": 70}),
                "sensor.bedroom_temp": _make_state("65"),
            }
        )
        ae = object.__new__(AutomationEngine)
        ae.hass = hass
        ae.climate_entity = "climate.thermostat"
        ae.config = self._sleep_config()
        ae._get_indoor_temp_f = types.MethodType(AutomationEngine._get_indoor_temp_f, ae)
        with patch(
            "custom_components.climate_advisor.automation.dt_util.now",
            return_value=_dt.datetime(2026, 1, 1, 23, 0),
        ):
            assert ae._get_indoor_temp_f() == 65.0

    def test_coord_uses_sleep_sensor_in_window(self):
        hass = _hass_with_entities(
            {
                "climate.thermostat": _make_state("heat", {"current_temperature": 70}),
                "sensor.bedroom_temp": _make_state("65"),
            }
        )
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord.hass = hass
        coord.config = self._sleep_config()
        coord._get_indoor_temp_with_provenance = types.MethodType(
            ClimateAdvisorCoordinator._get_indoor_temp_with_provenance, coord
        )
        coord._get_indoor_temp = types.MethodType(ClimateAdvisorCoordinator._get_indoor_temp, coord)
        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.now",
            return_value=_dt.datetime(2026, 1, 1, 23, 0),
        ):
            assert coord._get_indoor_temp() == 65.0

    def test_engine_uses_primary_outside_window(self):
        hass = _hass_with_entities(
            {
                "climate.thermostat": _make_state("heat", {"current_temperature": 70}),
                "sensor.bedroom_temp": _make_state("65"),
            }
        )
        ae = object.__new__(AutomationEngine)
        ae.hass = hass
        ae.climate_entity = "climate.thermostat"
        ae.config = self._sleep_config()
        ae._get_indoor_temp_f = types.MethodType(AutomationEngine._get_indoor_temp_f, ae)
        with patch(
            "custom_components.climate_advisor.automation.dt_util.now",
            return_value=_dt.datetime(2026, 1, 1, 12, 0),
        ):
            assert ae._get_indoor_temp_f() == 70.0

    def test_coord_uses_primary_when_sleep_entity_unset(self):
        hass = _hass_with_entities({"climate.thermostat": _make_state("heat", {"current_temperature": 70})})
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord.hass = hass
        coord.config = {**self._sleep_config(), "sleep_indoor_temp_entity": None}
        coord._get_indoor_temp_with_provenance = types.MethodType(
            ClimateAdvisorCoordinator._get_indoor_temp_with_provenance, coord
        )
        coord._get_indoor_temp = types.MethodType(ClimateAdvisorCoordinator._get_indoor_temp, coord)
        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.now",
            return_value=_dt.datetime(2026, 1, 1, 23, 0),
        ):
            assert coord._get_indoor_temp() == 70.0


class TestExistingBehaviorUnaffectedBySleepSensorRefactor:
    """Regression guard for the resolve_indoor_temp_f() extraction itself: calling
    without any sleep-window args must be byte-for-byte identical to prior behavior."""

    def test_resolve_indoor_temp_f_default_args_unchanged(self):
        hass = MagicMock()
        hass.states.get.return_value = _make_state("heat", {"current_temperature": 72})
        result = resolve_indoor_temp_f(
            hass=hass,
            source=TEMP_SOURCE_CLIMATE_FALLBACK,
            unit="fahrenheit",
            indoor_temp_entity=None,
            climate_entity="climate.thermostat",
        )
        assert result == 72.0
