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
