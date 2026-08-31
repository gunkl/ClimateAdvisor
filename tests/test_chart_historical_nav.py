"""TDD tests for Issue #160 — historical chart navigation via before_ts anchor.

Written BEFORE implementation. All four tests should FAIL against unmodified code.

Bug: chart back-nav renders blank because the viewport can drift before the loaded
data window.  Fix requires a before_ts/before anchor threaded through all four layers:
    frontend → API → get_chart_data() → ChartStateLog.get_entries()

Test classes:
  TestGetEntriesBeforeAnchor   — ChartStateLog.get_entries() with before= param
  TestGetChartDataBeforeTs     — coordinator.get_chart_data() with before_ts= param
"""

from __future__ import annotations

import importlib
import sys
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

# ── HA module stubs (must happen before importing climate_advisor) ──────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# Patch dt_util stubs so coordinator imports resolve correctly.
_FAKE_NOW = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
sys.modules["homeassistant.util.dt"].now = lambda: _FAKE_NOW
sys.modules["homeassistant.util.dt"].parse_datetime = lambda s: datetime.fromisoformat(s) if s else None
sys.modules["homeassistant.util.dt"].as_local = lambda x: x
sys.modules["homeassistant.util.dt"].DEFAULT_TIME_ZONE = UTC

from custom_components.climate_advisor.chart_log import ChartStateLog  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _now() -> datetime:
    return _FAKE_NOW


def _ago(**kwargs) -> datetime:
    return _now() - timedelta(**kwargs)


def _make_log(tmp_path, max_days: int = 365) -> ChartStateLog:
    from pathlib import Path

    return ChartStateLog(Path(tmp_path), max_days=max_days)


def _populate_three_entries(log: ChartStateLog) -> tuple[datetime, datetime, datetime]:
    """Add three entries at: now-5d, now-2d, now-1h. Return the three datetimes."""
    t_old = _ago(days=5)
    t_mid = _ago(days=2)
    t_recent = _ago(hours=1)
    log.append(hvac="heating", fan=False, indoor=65.0, outdoor=30.0, ts=_iso(t_old))
    log.append(hvac="off", fan=False, indoor=70.0, outdoor=50.0, ts=_iso(t_mid))
    log.append(hvac="off", fan=False, indoor=72.0, outdoor=55.0, ts=_iso(t_recent))
    return t_old, t_mid, t_recent


def _get_coordinator_class():
    """Return a fresh ClimateAdvisorCoordinator class reference."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _get_coordinator_module():
    return importlib.import_module("custom_components.climate_advisor.coordinator")


# ---------------------------------------------------------------------------
# Test 1: ChartStateLog.get_entries() respects before= anchor (upper bound)
# ---------------------------------------------------------------------------


class TestGetEntriesBeforeAnchor:
    """Test 1: get_entries() with a before= datetime argument.

    Expected: only entries strictly before the anchor are returned.
    FAILS before implementation because get_entries() has no before= param.
    """

    @pytest.fixture(autouse=True)
    def _freeze_chart_log_now(self):
        """Freeze chart_log.dt_util.now() to _FAKE_NOW for all tests in this class.

        chart_log.py binds dt_util via `from homeassistant.util import dt as dt_util`.
        In full-suite runs, earlier test files can overwrite sys.modules["homeassistant.util"].dt.now
        to return real wall-clock time, which breaks time-relative assertions here.
        Patching the attribute on the already-bound module object ensures isolation.
        """
        from custom_components.climate_advisor import chart_log as _chart_log_mod

        orig = _chart_log_mod.dt_util.now
        _chart_log_mod.dt_util.now = lambda: _FAKE_NOW
        yield
        _chart_log_mod.dt_util.now = orig

    def test_get_entries_before_anchor_filters_past_window(self, tmp_path):
        """Entries after the anchor must be excluded.

        Setup: 3 entries at now-5d, now-2d, now-1h.
        Call get_entries("3d", before=<3 days ago>).
        Expected: only the now-5d entry is returned (now-2d and now-1h are after the anchor).
        """
        log = _make_log(tmp_path)
        t_old, t_mid, t_recent = _populate_three_entries(log)

        anchor = _ago(days=3)
        result = log.get_entries("3d", before=anchor)

        assert len(result) == 1, (
            f"Expected 1 entry (only now-5d is before the anchor at now-3d); got {len(result)}. "
            "Entries returned: "
            + str([e.get("ts") for e in result])
            + f"\nAnchor: {anchor.isoformat()}, entries at: {t_old.isoformat()}, "
            f"{t_mid.isoformat()}, {t_recent.isoformat()}"
        )
        returned_ts = result[0].get("ts", "")
        entry_dt = datetime.fromisoformat(returned_ts).replace(tzinfo=UTC) if returned_ts else None
        assert entry_dt is not None and entry_dt <= anchor, (
            f"Returned entry ts={returned_ts!r} is not before anchor {anchor.isoformat()}"
        )

    def test_get_entries_no_before_returns_all_in_range(self, tmp_path):
        """Regression: without before=, get_entries('3d') returns entries in the 3-day window.

        Setup: 3 entries at now-5d, now-2d, now-1h.
        Call get_entries("3d") with no before argument.
        Expected: 2 entries (now-2d and now-1h are within 3 days of now; now-5d is outside).
        PASSES before and after implementation — regression guard for existing behavior.
        """
        log = _make_log(tmp_path)
        _populate_three_entries(log)

        result = log.get_entries("3d")

        assert len(result) == 2, (
            f"Expected 2 entries within 3 days (now-2d, now-1h); got {len(result)}. "
            "Entries: " + str([e.get("ts") for e in result])
        )


# ---------------------------------------------------------------------------
# Test 3 & 4: coordinator.get_chart_data() with before_ts= param
# ---------------------------------------------------------------------------


class TestGetChartDataBeforeTs:
    """Tests for get_chart_data(before_ts=...) on the coordinator.

    FAILS before implementation because get_chart_data() has no before_ts param.
    """

    @pytest.fixture(autouse=True)
    def _freeze_chart_log_now(self):
        """Freeze chart_log.dt_util.now() to _FAKE_NOW for all tests in this class."""
        from custom_components.climate_advisor import chart_log as _chart_log_mod

        orig = _chart_log_mod.dt_util.now
        _chart_log_mod.dt_util.now = lambda: _FAKE_NOW
        yield
        _chart_log_mod.dt_util.now = orig

    def _make_coord_with_chart_log(self, tmp_path):
        """Build a minimal coordinator stub with a populated _chart_log."""
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)

        # Minimal config
        coord.config = {
            "temp_unit": "fahrenheit",
            "comfort_heat": 68,
            "comfort_cool": 76,
            "setback_heat": 60,
            "setback_cool": 80,
        }

        # Stub learning (returns empty thermal model)
        mock_learning = MagicMock()
        mock_learning.get_thermal_model = MagicMock(
            return_value={
                "confidence": "none",
                "confidence_k_passive": "none",
                "observation_count_heat": 0,
                "observation_count_cool": 0,
                "observation_count_passive": 0,
                "observation_count_fan_only": 0,
                "observation_count_vent": 0,
                "observation_count_solar": 0,
                "observation_count_swing_heat": 0,
                "observation_count_swing_cool": 0,
                "heating_rate_f_per_hour": None,
                "cooling_rate_f_per_hour": None,
                "k_passive": None,
                "k_vent": None,
                "k_vent_window": None,
                "k_solar": None,
                "learning_health": {},
                "swing_heat_f_display": 1.5,
                "swing_cool_f_display": 1.5,
                "swing_heat_f": None,
                "swing_cool_f": None,
                "confidence_swing_heat": "none",
                "confidence_swing_cool": "none",
                "solar_phase_offset_h": None,
                "avg_r_squared_passive": None,
                "last_observation_date": None,
            }
        )
        coord.learning = mock_learning

        # Stub hass for _build_learning_health
        coord.hass = MagicMock()

        # Forecast and classification
        coord._hourly_forecast_temps = []
        coord._current_classification = None
        coord._occupancy_mode = "home"

        # Build a real chart log with three entries
        from pathlib import Path

        chart_log = ChartStateLog(Path(tmp_path), max_days=365)
        t_old, t_mid, t_recent = _populate_three_entries(chart_log)
        coord._chart_log = chart_log

        # Bind get_chart_data as an instance method
        coord.get_chart_data = types.MethodType(ClimateAdvisorCoordinator.get_chart_data, coord)
        # Bind _build_learning_health
        coord._build_learning_health = types.MethodType(ClimateAdvisorCoordinator._build_learning_health, coord)
        # Bind _get_indoor_temp
        coord._get_indoor_temp = MagicMock(return_value=None)

        return coord, t_old, t_mid, t_recent

    def test_get_chart_data_before_ts_filters_state_log(self, tmp_path):
        """get_chart_data(before_ts=...) must pass before= to chart_log.get_entries().

        Setup: chart log with entries at now-5d, now-2d, now-1h.
        Call get_chart_data("3d", before_ts=<(now - 3 days).timestamp() * 1000>).
        Expected:
          - state_log contains only the now-5d entry (before the anchor)
          - predicted_indoor == [] (historical view suppresses forecast)
          - forecast_outdoor == [] (historical view suppresses forecast)
        FAILS before implementation (no before_ts param, no historical suppression).
        """
        coord, t_old, t_mid, t_recent = self._make_coord_with_chart_log(tmp_path)
        anchor_dt = _ago(days=3)
        # Frontend sends ms; API divides by 1000 before passing to coordinator
        # So coordinator receives seconds
        before_ts_seconds = anchor_dt.timestamp()

        with (
            patch(
                "custom_components.climate_advisor.coordinator.dt_util.as_local",
                side_effect=lambda x: x,
            ),
            patch(
                "custom_components.climate_advisor.coordinator.dt_util.now",
                return_value=_FAKE_NOW,
            ),
        ):
            result = coord.get_chart_data("3d", before_ts=before_ts_seconds)

        state_log = result.get("state_log", [])
        assert len(state_log) == 1, (
            f"Expected 1 state_log entry (only now-5d is before anchor); got {len(state_log)}. "
            "state_log ts: " + str([e.get("ts") for e in state_log])
        )

        predicted_indoor = result.get("predicted_indoor", None)
        assert predicted_indoor == [], (
            f"Historical view must suppress predicted_indoor (expected []); got {predicted_indoor!r}"
        )

        forecast_outdoor = result.get("forecast_outdoor", None)
        assert forecast_outdoor == [], (
            f"Historical view must suppress forecast_outdoor (expected []); got {forecast_outdoor!r}"
        )

    def test_get_chart_data_before_ts_none_returns_recent_entries(self, tmp_path):
        """Regression: get_chart_data() without before_ts returns recent entries.

        Setup: chart log with entries at now-5d, now-2d, now-1h.
        Call get_chart_data("3d") with no before_ts.
        Expected:
          - state_log contains the 2 entries within 3 days (now-2d, now-1h)
          - predicted_indoor is a list (not suppressed)
        PASSES before and after implementation — regression guard for existing behavior.
        """
        coord, t_old, t_mid, t_recent = self._make_coord_with_chart_log(tmp_path)

        with (
            patch(
                "custom_components.climate_advisor.coordinator.dt_util.as_local",
                side_effect=lambda x: x,
            ),
            patch(
                "custom_components.climate_advisor.coordinator.dt_util.now",
                return_value=_FAKE_NOW,
            ),
        ):
            result = coord.get_chart_data("3d")

        state_log = result.get("state_log", [])
        assert len(state_log) == 2, (
            f"Expected 2 state_log entries within 3 days; got {len(state_log)}. "
            "state_log ts: " + str([e.get("ts") for e in state_log])
        )

        predicted_indoor = result.get("predicted_indoor", None)
        assert isinstance(predicted_indoor, list), (
            f"predicted_indoor must be a list (not suppressed); got {type(predicted_indoor).__name__}"
        )


# ---------------------------------------------------------------------------
# Test 5: get_chart_data() target_band reads real historical lower/upper
# (Issue #514 — historical band persistence, Phase 2 step 8)
# ---------------------------------------------------------------------------


class TestGetChartDataHistoricalTargetBand:
    """A historical viewport must show what the target band actually was at that
    past time — read from the immutable per-cycle lower/upper snapshot persisted
    into chart_log (Phase 2 steps 6-7) — not today's live band recomputed against
    today's config/occupancy/classification (the pre-#514 behavior, where
    ``target_band`` was the one series NOT gated by ``is_historical``)."""

    @pytest.fixture(autouse=True)
    def _freeze_chart_log_now(self):
        from custom_components.climate_advisor import chart_log as _chart_log_mod

        orig = _chart_log_mod.dt_util.now
        _chart_log_mod.dt_util.now = lambda: _FAKE_NOW
        yield
        _chart_log_mod.dt_util.now = orig

    def _make_coord_with_banded_chart_log(self, tmp_path):
        """Same stub shape as TestGetChartDataBeforeTs._make_coord_with_chart_log(),
        but entries carry distinct lower/upper values so a historical read-back can
        be proven to match the OLD entry's band, not today's live (very different)
        comfort-band config."""
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)

        # Today's LIVE config band is intentionally very different from what was
        # persisted historically (60-90 vs. the persisted 66/74) — proves a
        # historical request doesn't fall through to a live recompute.
        coord.config = {
            "temp_unit": "fahrenheit",
            "comfort_heat": 60,
            "comfort_cool": 90,
            "setback_heat": 55,
            "setback_cool": 95,
        }

        mock_learning = MagicMock()
        mock_learning.get_thermal_model = MagicMock(
            return_value={
                "confidence": "none",
                "confidence_k_passive": "none",
                "observation_count_heat": 0,
                "observation_count_cool": 0,
                "observation_count_passive": 0,
                "observation_count_fan_only": 0,
                "observation_count_vent": 0,
                "observation_count_solar": 0,
                "observation_count_swing_heat": 0,
                "observation_count_swing_cool": 0,
                "heating_rate_f_per_hour": None,
                "cooling_rate_f_per_hour": None,
                "k_passive": None,
                "k_vent": None,
                "k_vent_window": None,
                "k_solar": None,
                "learning_health": {},
                "swing_heat_f_display": 1.5,
                "swing_cool_f_display": 1.5,
                "swing_heat_f": None,
                "swing_cool_f": None,
                "confidence_swing_heat": "none",
                "confidence_swing_cool": "none",
                "solar_phase_offset_h": None,
                "avg_r_squared_passive": None,
                "last_observation_date": None,
            }
        )
        coord.learning = mock_learning
        coord.hass = MagicMock()
        coord._hourly_forecast_temps = []
        coord._current_classification = None
        coord._occupancy_mode = "home"

        from pathlib import Path

        chart_log = ChartStateLog(Path(tmp_path), max_days=365)
        t_old = _ago(days=5)
        t_recent = _ago(hours=1)
        # The historical entry's persisted band (what the fix must read back).
        chart_log.append(hvac="heat", fan=False, indoor=65.0, outdoor=30.0, lower=66.0, upper=74.0, ts=_iso(t_old))
        chart_log.append(hvac="off", fan=False, indoor=72.0, outdoor=55.0, lower=68.0, upper=76.0, ts=_iso(t_recent))
        coord._chart_log = chart_log

        coord.get_chart_data = types.MethodType(ClimateAdvisorCoordinator.get_chart_data, coord)
        coord._build_learning_health = types.MethodType(ClimateAdvisorCoordinator._build_learning_health, coord)
        coord._get_indoor_temp = MagicMock(return_value=None)

        return coord, t_old, t_recent

    def test_historical_target_band_matches_persisted_entry_not_live_config(self, tmp_path):
        coord, t_old, _t_recent = self._make_coord_with_banded_chart_log(tmp_path)
        anchor_dt = _ago(days=3)
        before_ts_seconds = anchor_dt.timestamp()

        with (
            patch(
                "custom_components.climate_advisor.coordinator.dt_util.as_local",
                side_effect=lambda x: x,
            ),
            patch(
                "custom_components.climate_advisor.coordinator.dt_util.now",
                return_value=_FAKE_NOW,
            ),
        ):
            result = coord.get_chart_data("3d", before_ts=before_ts_seconds)

        target_band = result.get("target_band", [])
        assert len(target_band) == 1, (
            f"Expected 1 historical target_band entry (only now-5d is before the "
            f"anchor); got {len(target_band)}: {target_band!r}"
        )
        entry = target_band[0]
        # Persisted values (66/74 °F) round-trip through _conv() unit conversion,
        # but config is fahrenheit here so they pass through unchanged.
        assert entry["lower"] == 66.0, (
            f"Historical target_band lower={entry['lower']!r} must match the persisted "
            "chart_log entry's lower (66.0), not today's live config-derived band "
            "(comfort_heat=60/setback_heat=55) — if this reads 60.0 or 55.0, the fix "
            "is falling through to a live recompute instead of state_log."
        )
        assert entry["upper"] == 74.0, (
            f"Historical target_band upper={entry['upper']!r} must match the persisted "
            "chart_log entry's upper (74.0), not today's live config-derived band "
            "(comfort_cool=90/setback_cool=95)."
        )

    def test_live_target_band_still_uses_current_config_when_not_historical(self, tmp_path):
        """Regression: a non-historical (live/"now") request must still compute the
        band from today's live config via _build_target_band_for() — proving the
        is_historical branch didn't accidentally become the only path."""
        coord, _t_old, _t_recent = self._make_coord_with_banded_chart_log(tmp_path)

        with (
            patch(
                "custom_components.climate_advisor.coordinator.dt_util.as_local",
                side_effect=lambda x: x,
            ),
            patch(
                "custom_components.climate_advisor.coordinator.dt_util.now",
                return_value=_FAKE_NOW,
            ),
        ):
            result = coord.get_chart_data("24h")

        target_band = result.get("target_band", [])
        # With no hourly forecast temps, _build_target_band_for() has nothing to
        # iterate — the live path returns an empty band, distinctly NOT the
        # persisted historical 66.0/74.0 values (proving it didn't read state_log).
        assert target_band == [], (
            f"Live (non-historical) target_band must come from _build_target_band_for() "
            f"(empty here — no forecast timestamps), not the persisted historical "
            f"entries; got {target_band!r}"
        )
