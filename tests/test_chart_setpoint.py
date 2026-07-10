"""TDD red-phase tests for chart setpoint overlay (Parts 4a, 4b, 4c).

Part 4a — ChartStateLog.append() accepts a `setpoint` parameter.
Part 4b — get_chart_data() returns "predicted_setpoint" (or a helper
           _derive_predicted_setpoint exists in coordinator.py).
Part 4c — get_chart_data() returns "historical_setpoint" (or a helper
           _extract_historical_setpoint exists in coordinator.py).

All tests must fail for the right reason (AttributeError / AssertionError /
ImportError), NOT for import-level failures.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Stub homeassistant before importing chart_log
# The same pattern used in test_chart_log.py — use setdefault so conftest
# MagicMocks are respected if already installed.
# ---------------------------------------------------------------------------


def _build_ha_stubs() -> None:
    if "homeassistant" in sys.modules:
        return

    ha = types.ModuleType("homeassistant")
    ha_util = types.ModuleType("homeassistant.util")
    ha_util_dt = types.ModuleType("homeassistant.util.dt")
    ha_util_dt.now = lambda: datetime.now(UTC)
    ha_util.dt = ha_util_dt
    ha.util = ha_util

    sys.modules.setdefault("homeassistant", ha)
    sys.modules.setdefault("homeassistant.util", ha_util)
    sys.modules.setdefault("homeassistant.util.dt", ha_util_dt)


_build_ha_stubs()

from custom_components.climate_advisor.chart_log import ChartStateLog  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _now() -> datetime:
    return datetime.now(UTC)


def _ago(**kwargs) -> datetime:
    return _now() - timedelta(**kwargs)


def _make_log(tmp_path: Path, max_days: int = 365) -> ChartStateLog:
    return ChartStateLog(tmp_path, max_days=max_days)


# ===========================================================================
# Part 4a — ChartStateLog.append() setpoint parameter
# ===========================================================================


class TestAppendSetpoint:
    """append() must accept and persist a setpoint keyword argument."""

    def test_setpoint_stored_when_provided(self, tmp_path: Path) -> None:
        """append(setpoint=68.0) → entry["setpoint"] == 68.0."""
        log = _make_log(tmp_path)
        log.append(hvac="heating", fan=False, indoor=68.0, outdoor=35.0, setpoint=68.0)
        entry = log._entries[0]
        assert entry["setpoint"] == 68.0

    def test_setpoint_none_when_explicitly_none(self, tmp_path: Path) -> None:
        """append(setpoint=None) → entry has setpoint key with value None."""
        log = _make_log(tmp_path)
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=50.0, setpoint=None)
        entry = log._entries[0]
        # Key must be present and value must be None (not missing)
        assert "setpoint" in entry
        assert entry["setpoint"] is None

    def test_no_crash_when_setpoint_omitted(self, tmp_path: Path) -> None:
        """Calling append() without setpoint must not raise."""
        log = _make_log(tmp_path)
        # Should not raise TypeError
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=50.0)
        assert log.entry_count == 1

    def test_get_entries_raw_includes_setpoint(self, tmp_path: Path) -> None:
        """get_entries() on a raw range exposes the setpoint value."""
        log = _make_log(tmp_path)
        log.append(
            hvac="heating",
            fan=False,
            indoor=68.0,
            outdoor=35.0,
            setpoint=68.0,
            ts=_iso(_ago(hours=1)),
        )
        result = log.get_entries("24h")
        assert len(result) == 1
        assert result[0].get("setpoint") == 68.0

    def test_bucket_hourly_setpoint_mean_two_values(self, tmp_path: Path) -> None:
        """_bucket_hourly with two entries (68.0, 70.0) → setpoint == 69.0."""
        log = _make_log(tmp_path)
        base = _ago(hours=2).replace(minute=0, second=0, microsecond=0)
        log.append(
            hvac="heating",
            fan=False,
            indoor=68.0,
            outdoor=35.0,
            setpoint=68.0,
            ts=_iso(base.replace(minute=10)),
        )
        log.append(
            hvac="heating",
            fan=False,
            indoor=70.0,
            outdoor=35.0,
            setpoint=70.0,
            ts=_iso(base.replace(minute=40)),
        )
        result = log.get_entries("7d")
        assert len(result) == 1
        assert result[0].get("setpoint") == 69.0

    def test_bucket_hourly_setpoint_ignores_none(self, tmp_path: Path) -> None:
        """_bucket_hourly with one entry setpoint=68.0, one setpoint=None → setpoint == 68.0."""
        log = _make_log(tmp_path)
        base = _ago(hours=2).replace(minute=0, second=0, microsecond=0)
        log.append(
            hvac="heating",
            fan=False,
            indoor=68.0,
            outdoor=35.0,
            setpoint=68.0,
            ts=_iso(base.replace(minute=10)),
        )
        log.append(
            hvac="heating",
            fan=False,
            indoor=70.0,
            outdoor=35.0,
            setpoint=None,
            ts=_iso(base.replace(minute=40)),
        )
        result = log.get_entries("7d")
        assert len(result) == 1
        assert result[0].get("setpoint") == 68.0

    def test_bucket_daily_includes_setpoint_mean(self, tmp_path: Path) -> None:
        """_bucket_daily with two entries → daily bucket contains 'setpoint' field."""
        log = _make_log(tmp_path)
        base = _ago(days=60).replace(hour=10, minute=0, second=0, microsecond=0)
        log.append(
            hvac="heating",
            fan=False,
            indoor=68.0,
            outdoor=32.0,
            setpoint=68.0,
            ts=_iso(base),
        )
        log.append(
            hvac="heating",
            fan=False,
            indoor=70.0,
            outdoor=33.0,
            setpoint=70.0,
            ts=_iso(base.replace(hour=14)),
        )
        result = log.get_entries("1y")
        assert len(result) == 1
        day = result[0]
        assert "setpoint" in day
        assert day["setpoint"] == 69.0


# ===========================================================================
# Part 4b — _derive_predicted_setpoint helper in coordinator.py
# ===========================================================================


class TestDerivePredictedSetpoint:
    """coordinator.py must expose _derive_predicted_setpoint(target_band, hvac_mode)."""

    def _import_helper(self):
        """Import _derive_predicted_setpoint from coordinator.py."""
        # Delay import so the test module loads even when the function is absent.
        # The AttributeError/ImportError IS the expected red-phase failure.
        import importlib

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        return mod._derive_predicted_setpoint

    def _make_band(self, entries: list[tuple[str, float, float]]) -> list[dict]:
        """Build a target_band list: [(ts, lower, upper)]."""
        return [{"ts": ts, "lower": lower, "upper": upper} for ts, lower, upper in entries]

    def test_heat_mode_returns_lower_bound(self) -> None:
        """Heat mode → each entry gets setpoint == lower."""
        fn = self._import_helper()
        band = self._make_band(
            [
                ("2026-05-17T06:00:00+00:00", 68.0, 74.0),
                ("2026-05-17T07:00:00+00:00", 68.0, 74.0),
            ]
        )
        result = fn(band, "heat")
        assert len(result) == 2
        assert result[0]["setpoint"] == 68.0
        assert result[1]["setpoint"] == 68.0
        assert result[0]["ts"] == "2026-05-17T06:00:00+00:00"

    def test_cool_mode_returns_upper_bound(self) -> None:
        """Cool mode → each entry gets setpoint == upper."""
        fn = self._import_helper()
        band = self._make_band(
            [
                ("2026-05-17T14:00:00+00:00", 70.0, 76.0),
                ("2026-05-17T15:00:00+00:00", 70.0, 76.0),
            ]
        )
        result = fn(band, "cool")
        assert len(result) == 2
        assert result[0]["setpoint"] == 76.0
        assert result[1]["setpoint"] == 76.0

    def test_off_mode_returns_none_for_all(self) -> None:
        """Off mode → every entry has setpoint == None."""
        fn = self._import_helper()
        band = self._make_band(
            [
                ("2026-05-17T10:00:00+00:00", 68.0, 74.0),
                ("2026-05-17T11:00:00+00:00", 69.0, 75.0),
            ]
        )
        result = fn(band, "off")
        assert all(e["setpoint"] is None for e in result)

    def test_none_hvac_mode_returns_none_for_all(self) -> None:
        """None hvac_mode → every entry has setpoint == None."""
        fn = self._import_helper()
        band = self._make_band([("2026-05-17T10:00:00+00:00", 68.0, 74.0)])
        result = fn(band, None)
        assert result[0]["setpoint"] is None

    def test_empty_band_returns_empty_list(self) -> None:
        """Empty target_band → empty result."""
        fn = self._import_helper()
        result = fn([], "heat")
        assert result == []

    def test_predicted_setpoint_key_in_get_chart_data(self) -> None:
        """get_chart_data() return dict must include 'predicted_setpoint' key."""
        # This is an integration smoke test — we only verify the key exists.
        # Full coordinator instantiation is skipped; we reach the return dict
        # by checking that the key name appears in coordinator source.
        import importlib

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        # The key must be referenced in the module source
        import inspect

        src = inspect.getsource(mod.ClimateAdvisorCoordinator.get_chart_data)
        assert "predicted_setpoint" in src, (
            "get_chart_data() does not return 'predicted_setpoint' — Part 4b not implemented"
        )


# ===========================================================================
# Part 4c — _extract_historical_setpoint helper in coordinator.py
# ===========================================================================


class TestExtractHistoricalSetpoint:
    """coordinator.py must expose _extract_historical_setpoint(log_entries)."""

    def _import_helper(self):
        import importlib

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        return mod._extract_historical_setpoint

    def test_extracts_ts_and_setpoint_pairs(self) -> None:
        """Returns [{ts, setpoint}] for each log entry."""
        fn = self._import_helper()
        entries = [
            {"ts": "2026-05-17T10:00:00+00:00", "setpoint": 68.0, "indoor": 68.5},
            {"ts": "2026-05-17T10:30:00+00:00", "setpoint": 69.0, "indoor": 69.1},
        ]
        result = fn(entries)
        assert len(result) == 2
        assert result[0] == {"ts": "2026-05-17T10:00:00+00:00", "setpoint": 68.0}
        assert result[1] == {"ts": "2026-05-17T10:30:00+00:00", "setpoint": 69.0}

    def test_preserves_none_setpoint(self) -> None:
        """Entries with setpoint=None → None preserved in output."""
        fn = self._import_helper()
        entries = [{"ts": "2026-05-17T12:00:00+00:00", "setpoint": None, "indoor": 70.0}]
        result = fn(entries)
        assert result[0]["setpoint"] is None

    def test_missing_setpoint_key_treated_as_none(self) -> None:
        """Older log entries without 'setpoint' key → setpoint None in output."""
        fn = self._import_helper()
        entries = [{"ts": "2026-05-17T12:00:00+00:00", "indoor": 70.0}]  # no setpoint key
        result = fn(entries)
        assert len(result) == 1
        assert result[0]["setpoint"] is None

    def test_empty_entries_returns_empty_list(self) -> None:
        """Empty input → empty output."""
        fn = self._import_helper()
        assert fn([]) == []

    def test_historical_setpoint_key_in_get_chart_data(self) -> None:
        """get_chart_data() return dict must include 'historical_setpoint' key."""
        import importlib
        import inspect

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        src = inspect.getsource(mod.ClimateAdvisorCoordinator.get_chart_data)
        assert "historical_setpoint" in src, (
            "get_chart_data() does not return 'historical_setpoint' — Part 4c not implemented"
        )


# ===========================================================================
# Issue #153 — _compute_defense_lines helper in coordinator.py
# ===========================================================================


class TestComputeDefenseLines:
    """coordinator.py must expose _compute_defense_lines(target_band).

    Always returns both heat (lower) and cool (upper) bounds regardless of HVAC mode.
    """

    def _import_helper(self):
        import importlib

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        return mod._compute_defense_lines

    def _make_band(self, entries: list[tuple[str, float, float]]) -> list[dict]:
        return [{"ts": ts, "lower": lower, "upper": upper} for ts, lower, upper in entries]

    def test_returns_heat_and_cool_keys(self) -> None:
        """Each entry has 'ts', 'heat', and 'cool' keys."""
        fn = self._import_helper()
        band = self._make_band([("2026-05-18T06:00:00+00:00", 68.0, 76.0)])
        result = fn(band)
        assert len(result) == 1
        assert set(result[0].keys()) == {"ts", "heat", "cool"}

    def test_heat_maps_to_lower_bound(self) -> None:
        """'heat' value == target_band 'lower' for each entry."""
        fn = self._import_helper()
        band = self._make_band(
            [
                ("2026-05-18T06:00:00+00:00", 68.0, 76.0),
                ("2026-05-18T22:00:00+00:00", 60.0, 76.0),  # setback at bedtime
            ]
        )
        result = fn(band)
        assert result[0]["heat"] == 68.0
        assert result[1]["heat"] == 60.0  # setback step visible

    def test_cool_maps_to_upper_bound(self) -> None:
        """'cool' value == target_band 'upper' for each entry."""
        fn = self._import_helper()
        band = self._make_band(
            [
                ("2026-05-18T06:00:00+00:00", 68.0, 76.0),
                ("2026-05-18T22:00:00+00:00", 68.0, 82.0),  # setback raises cool
            ]
        )
        result = fn(band)
        assert result[0]["cool"] == 76.0
        assert result[1]["cool"] == 82.0  # setback step visible

    def test_always_non_null_regardless_of_hvac_mode(self) -> None:
        """No hvac_mode argument — both bounds always populated."""
        fn = self._import_helper()
        band = self._make_band(
            [
                ("2026-05-18T10:00:00+00:00", 68.0, 76.0),
                ("2026-05-18T11:00:00+00:00", 68.0, 76.0),
            ]
        )
        result = fn(band)
        for entry in result:
            assert entry["heat"] is not None
            assert entry["cool"] is not None

    def test_empty_band_returns_empty_list(self) -> None:
        """Empty target_band → empty result."""
        fn = self._import_helper()
        assert fn([]) == []

    def test_ts_preserved(self) -> None:
        """Timestamps pass through unchanged."""
        fn = self._import_helper()
        ts = "2026-05-18T22:30:00+00:00"
        band = self._make_band([(ts, 60.0, 82.0)])
        result = fn(band)
        assert result[0]["ts"] == ts

    def test_defense_lines_key_in_get_chart_data(self) -> None:
        """get_chart_data() source must reference 'defense_lines' key."""
        import importlib
        import inspect

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        src = inspect.getsource(mod.ClimateAdvisorCoordinator.get_chart_data)
        assert "defense_lines" in src, (
            "get_chart_data() does not return 'defense_lines' — Issue #153 Part 1 not implemented"
        )


# ===========================================================================
# Issue #153 — _compute_predicted_activity helper in coordinator.py
# ===========================================================================


class TestComputePredictedActivity:
    """coordinator.py must expose _compute_predicted_activity(target_band, forecast_outdoor,
    predicted_indoor, classification, config).

    Returns [{ts, hvac_mode, fan_active, windows_recommended}] per forecast hour.
    """

    def _import_helper(self):
        import importlib

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        return mod._compute_predicted_activity

    def _make_band(self, ts: str, lower: float = 68.0, upper: float = 76.0) -> list[dict]:
        return [{"ts": ts, "lower": lower, "upper": upper}]

    def _make_forecast(self, ts: str, temp: float) -> list[dict]:
        return [{"ts": ts, "temp": temp}]

    def _make_predicted_indoor(self, ts: str, temp: float) -> list[dict]:
        return [{"ts": ts, "temp": temp}]

    def _make_classification(self, hvac_mode: str = "heat"):
        from unittest.mock import MagicMock

        c = MagicMock()
        c.hvac_mode = hvac_mode
        return c

    def _base_config(self, **overrides) -> dict:
        cfg = {
            "comfort_heat": 68.0,
            "comfort_cool": 76.0,
            "natural_vent_delta": 5.0,
            "fan_mode": "auto",
        }
        cfg.update(overrides)
        return cfg

    # --- output shape ---

    def test_returns_list_with_correct_keys(self) -> None:
        """Each entry has ts, hvac_mode, fan_active, windows_recommended."""
        fn = self._import_helper()
        ts = "2026-05-18T14:00:00+00:00"
        result = fn(
            self._make_band(ts),
            self._make_forecast(ts, 65.0),
            self._make_predicted_indoor(ts, 72.0),
            self._make_classification("heat"),
            self._base_config(),
        )
        assert len(result) == 1
        entry = result[0]
        assert set(entry.keys()) >= {"ts", "hvac_mode", "fan_active", "windows_recommended"}

    def test_empty_band_returns_empty_list(self) -> None:
        """Empty target_band → empty result."""
        fn = self._import_helper()
        result = fn([], [], [], None, self._base_config())
        assert result == []

    # --- hvac_mode ---

    def test_hvac_mode_from_classification(self) -> None:
        """hvac_mode in output matches classification.hvac_mode."""
        fn = self._import_helper()
        ts = "2026-05-18T14:00:00+00:00"
        result = fn(
            self._make_band(ts),
            self._make_forecast(ts, 65.0),
            self._make_predicted_indoor(ts, 72.0),
            self._make_classification("cool"),
            self._base_config(),
        )
        assert result[0]["hvac_mode"] == "cool"

    def test_hvac_mode_off_when_no_classification(self) -> None:
        """No classification (None) → hvac_mode == 'off'."""
        fn = self._import_helper()
        ts = "2026-05-18T14:00:00+00:00"
        result = fn(
            self._make_band(ts),
            self._make_forecast(ts, 72.0),
            self._make_predicted_indoor(ts, 72.0),
            None,  # no classification
            self._base_config(),
        )
        assert result[0]["hvac_mode"] == "off"

    # --- fan_active ---

    def test_fan_active_natural_vent_conditions(self) -> None:
        """Fan engages when outdoor < indoor AND indoor > comfort_cool."""
        fn = self._import_helper()
        ts = "2026-05-18T16:00:00+00:00"
        result = fn(
            self._make_band(ts, lower=68.0, upper=76.0),
            self._make_forecast(ts, 70.0),  # outdoor 70 < indoor 78
            self._make_predicted_indoor(ts, 78.0),  # indoor above cool comfort
            self._make_classification("off"),
            self._base_config(comfort_cool=76.0),
        )
        assert result[0]["fan_active"] is True

    def test_natural_vent_delta_default_matches_automation_default(self) -> None:
        """Architecture-reset latent-bug fix: when natural_vent_delta isn't explicitly
        configured, the chart prediction's fallback must match DEFAULT_NATURAL_VENT_DELTA
        (3.0F), the same default the real automation engine uses -- not a stray 5.0F that
        would let the chart predict fan activity the real engine wouldn't actually produce.

        band_upper=76, outdoor=78.5: with the old buggy 5.0F default, 78.5 < 76+5=81 is
        True (fan predicted active); with the fixed 3.0F default, 78.5 < 76+3=79 is still
        True but 79.5 would flip to False -- pinning the boundary at exactly the real
        engine's threshold, not the wider stray one.
        """
        fn = self._import_helper()
        ts = "2026-05-18T16:00:00+00:00"
        config_without_delta = {"comfort_heat": 68.0, "comfort_cool": 76.0, "fan_mode": "auto"}
        result = fn(
            self._make_band(ts, lower=68.0, upper=76.0),
            self._make_forecast(ts, 79.5),  # outdoor 79.5: below the buggy 81 threshold,
            # ABOVE the fixed 79 threshold -- distinguishes the two defaults
            self._make_predicted_indoor(ts, 85.0),  # indoor well above outdoor and band_upper
            self._make_classification("off"),
            config_without_delta,
        )
        assert result[0]["fan_active"] is False, (
            "with the fixed 3.0F default (threshold=79), outdoor=79.5 must NOT predict fan "
            "activity -- if this is True, the stray 5.0F default (threshold=81) regressed"
        )

    def test_fan_not_active_when_outdoor_warmer_than_indoor(self) -> None:
        """Fan off when outdoor >= indoor (no benefit from ventilation)."""
        fn = self._import_helper()
        ts = "2026-05-18T14:00:00+00:00"
        result = fn(
            self._make_band(ts, lower=68.0, upper=76.0),
            self._make_forecast(ts, 80.0),  # outdoor 80 > indoor 72
            self._make_predicted_indoor(ts, 72.0),
            self._make_classification("off"),
            self._base_config(),
        )
        assert result[0]["fan_active"] is False

    def test_fan_active_when_fan_mode_on(self) -> None:
        """Fan always active when fan_mode='on' (continuous circulation)."""
        fn = self._import_helper()
        ts = "2026-05-18T02:00:00+00:00"
        result = fn(
            self._make_band(ts, lower=60.0, upper=82.0),
            self._make_forecast(ts, 55.0),  # cold outdoor — would normally not trigger
            self._make_predicted_indoor(ts, 65.0),  # indoor below comfort_cool
            self._make_classification("heat"),
            self._base_config(fan_mode="on"),
        )
        assert result[0]["fan_active"] is True

    # --- windows_recommended ---

    def test_windows_recommended_when_outdoor_pleasant_indoor_warm(self) -> None:
        """Windows recommended when outdoor in comfort zone and indoor is above comfort_cool."""
        fn = self._import_helper()
        ts = "2026-05-18T08:00:00+00:00"
        result = fn(
            self._make_band(ts, lower=68.0, upper=76.0),
            self._make_forecast(ts, 72.0),  # outdoor in comfort zone
            self._make_predicted_indoor(ts, 79.0),  # indoor above cool ceiling
            self._make_classification("off"),
            self._base_config(comfort_heat=68.0, comfort_cool=76.0),
        )
        assert result[0]["windows_recommended"] is True

    def test_windows_not_recommended_when_outdoor_too_cold(self) -> None:
        """Windows not recommended when outdoor below comfort_heat."""
        fn = self._import_helper()
        ts = "2026-05-18T06:00:00+00:00"
        result = fn(
            self._make_band(ts, lower=68.0, upper=76.0),
            self._make_forecast(ts, 45.0),  # outdoor too cold
            self._make_predicted_indoor(ts, 72.0),
            self._make_classification("heat"),
            self._base_config(comfort_heat=68.0, comfort_cool=76.0),
        )
        assert result[0]["windows_recommended"] is False

    def test_windows_not_recommended_when_outdoor_warmer_than_indoor(self) -> None:
        """Windows not recommended when outdoor >= indoor (would heat the house)."""
        fn = self._import_helper()
        ts = "2026-05-18T15:00:00+00:00"
        result = fn(
            self._make_band(ts, lower=68.0, upper=76.0),
            self._make_forecast(ts, 82.0),  # outdoor 82 > indoor 78
            self._make_predicted_indoor(ts, 78.0),
            self._make_classification("off"),
            self._base_config(),
        )
        assert result[0]["windows_recommended"] is False

    def test_predicted_activity_key_in_get_chart_data(self) -> None:
        """get_chart_data() source must reference 'predicted_activity' key."""
        import importlib
        import inspect

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        src = inspect.getsource(mod.ClimateAdvisorCoordinator.get_chart_data)
        assert "predicted_activity" in src, (
            "get_chart_data() does not return 'predicted_activity' — Issue #153 Part 2 not implemented"
        )
