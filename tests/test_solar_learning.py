"""Tests for k_solar confidence ladder and solar phase fit observability (Issue #308).

Phase A: get_thermal_model() confidence_k_solar ladder — currently hardcoded 'none', must
         return graded confidence based on observation_count_solar.
Phase B: _run_solar_phase_chart_log_fit() structured INFO logging — method-level summaries
         and EWMA update lines must appear in coordinator logs.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from datetime import UTC

from custom_components.climate_advisor.learning import LearningEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine(tmp_path: Path) -> LearningEngine:
    engine = LearningEngine(tmp_path)
    engine.load_state()
    return engine


def _set_solar_obs_count(engine: LearningEngine, count: int, k_solar: float = 0.5) -> None:
    """Directly inject observation_count_solar and k_solar into the thermal cache."""
    if engine._state.thermal_model_cache is None:
        engine._state.thermal_model_cache = {}
    cache = engine._state.thermal_model_cache
    cache["observation_count_solar"] = count
    cache["k_solar"] = k_solar
    # Ensure k_passive is present with a valid value so we don't accidentally test
    # other confidence paths.
    cache.setdefault("k_passive", -0.08)
    cache.setdefault("observation_count_passive", 5)


# ---------------------------------------------------------------------------
# Phase A — k_solar confidence ladder
# ---------------------------------------------------------------------------


class TestKSolarConfidenceLadder:
    """get_thermal_model() must return graded confidence_k_solar, not hardcoded 'none'."""

    def test_k_solar_confidence_none_when_zero_obs(self, tmp_path: Path):
        """Zero observations → confidence_k_solar must be 'none'."""
        engine = _make_engine(tmp_path)
        model = engine.get_thermal_model()
        assert model.get("confidence_k_solar") == "none", (
            f"Expected 'none' with 0 solar obs, got {model.get('confidence_k_solar')!r}"
        )

    def test_k_solar_confidence_none_below_threshold(self, tmp_path: Path):
        """19 observations is below the 20-obs 'low' threshold — still 'none'."""
        engine = _make_engine(tmp_path)
        _set_solar_obs_count(engine, 19)
        model = engine.get_thermal_model()
        assert model.get("confidence_k_solar") == "none", (
            f"Expected 'none' at 19 obs, got {model.get('confidence_k_solar')!r}"
        )

    def test_k_solar_confidence_low_at_20(self, tmp_path: Path):
        """20 observations → confidence_k_solar = 'low'."""
        engine = _make_engine(tmp_path)
        _set_solar_obs_count(engine, 20)
        model = engine.get_thermal_model()
        assert model.get("confidence_k_solar") == "low", (
            f"Expected 'low' at 20 obs, got {model.get('confidence_k_solar')!r}"
        )

    def test_k_solar_confidence_medium_at_50(self, tmp_path: Path):
        """50 observations → confidence_k_solar = 'medium'."""
        engine = _make_engine(tmp_path)
        _set_solar_obs_count(engine, 50)
        model = engine.get_thermal_model()
        assert model.get("confidence_k_solar") == "medium", (
            f"Expected 'medium' at 50 obs, got {model.get('confidence_k_solar')!r}"
        )

    def test_k_solar_confidence_high_at_100(self, tmp_path: Path):
        """100 observations → confidence_k_solar = 'high'."""
        engine = _make_engine(tmp_path)
        _set_solar_obs_count(engine, 100)
        model = engine.get_thermal_model()
        assert model.get("confidence_k_solar") == "high", (
            f"Expected 'high' at 100 obs, got {model.get('confidence_k_solar')!r}"
        )

    def test_confidence_k_solar_alias_present(self, tmp_path: Path):
        """'confidence_k_solar' key must exist in the model dict."""
        engine = _make_engine(tmp_path)
        model = engine.get_thermal_model()
        assert "confidence_k_solar" in model, (
            f"'confidence_k_solar' key missing from get_thermal_model() output. Keys: {list(model)}"
        )

    def test_confidence_k_solar_alias_matches(self, tmp_path: Path):
        """confidence_k_solar must equal the solar confidence value (not a stale 'none')."""
        engine = _make_engine(tmp_path)
        _set_solar_obs_count(engine, 50)
        model = engine.get_thermal_model()
        # The alias and the graded value must agree
        assert model["confidence_k_solar"] == "medium", (
            f"confidence_k_solar should be 'medium' at 50 obs, got {model['confidence_k_solar']!r}"
        )

    def test_k_solar_confidence_boundary_49(self, tmp_path: Path):
        """49 observations is one below the 50-obs 'medium' threshold — must still be 'low'."""
        engine = _make_engine(tmp_path)
        _set_solar_obs_count(engine, 49)
        model = engine.get_thermal_model()
        assert model.get("confidence_k_solar") == "low", (
            f"Expected 'low' at 49 obs, got {model.get('confidence_k_solar')!r}"
        )

    def test_k_solar_confidence_boundary_99(self, tmp_path: Path):
        """99 observations is one below the 100-obs 'high' threshold — must still be 'medium'."""
        engine = _make_engine(tmp_path)
        _set_solar_obs_count(engine, 99)
        model = engine.get_thermal_model()
        assert model.get("confidence_k_solar") == "medium", (
            f"Expected 'medium' at 99 obs, got {model.get('confidence_k_solar')!r}"
        )


# ---------------------------------------------------------------------------
# Phase B — solar phase fit structured logging
# ---------------------------------------------------------------------------
# These tests exercise _run_solar_phase_chart_log_fit() on the coordinator.
# Because the coordinator has many dependencies, we use a lightweight stub
# approach: build a minimal coordinator-like object with just enough state for
# the method to run, and patch dt_util so datetime comparisons work.
#
# NOTE: If the coordinator fixture becomes too complex to maintain here,
# Phase B logging can be verified via post-deploy ha_logs.py inspection.
# The Phase A tests are the primary automated coverage gate for this issue.


def _make_chart_log_entry(
    ts: str,
    indoor: float = 72.0,
    outdoor: float = 68.0,
    hvac: str = "off",
    fan: str = "off",
    windows_open: bool = False,
) -> dict:
    return {
        "ts": ts,
        "indoor": indoor,
        "outdoor": outdoor,
        "hvac": hvac,
        "fan": fan,
        "windows_open": windows_open,
    }


class TestSolarPhaseFitLogging:
    """_run_solar_phase_chart_log_fit() must emit INFO-level structured log lines."""

    def _make_minimal_coordinator(self, entries: list[dict]) -> MagicMock:
        """Build a minimal coordinator stand-in with a chart_log stub."""
        chart_log = MagicMock()
        chart_log._entries = entries

        coord = MagicMock()
        coord._chart_log = chart_log
        return coord

    def test_solar_phase_fit_logs_entry_with_zero_windows(self, caplog):
        """When no qualifying daytime-passive windows exist, an INFO line noting 0 windows
        must be emitted rather than the method silently returning."""
        from custom_components.climate_advisor import coordinator as coord_mod

        # Build a chart_log with entries that are all nighttime (hour 2), so no
        # qualifying daytime windows will be found.
        night_entries = [
            _make_chart_log_entry("2026-06-10T02:00:00+00:00"),
            _make_chart_log_entry("2026-06-10T02:30:00+00:00"),
        ]

        chart_log_stub = MagicMock()
        chart_log_stub._entries = night_entries

        # Patch dt_util.now() and dt_util.as_local() so datetime arithmetic works
        from datetime import datetime

        fixed_now = datetime(2026, 6, 12, 18, 0, 0, tzinfo=UTC)

        def _as_local(dt):
            return dt  # Return UTC as-is; hour=2 is still nighttime

        with (
            patch.object(coord_mod, "dt_util") as mock_dt,
            patch(
                "custom_components.climate_advisor.coordinator._estimate_solar_phase_offset",
                return_value=(None, "test_reject"),
            ),
        ):
            mock_dt.now.return_value = fixed_now
            mock_dt.as_local.side_effect = _as_local

            # Build a real coordinator method call via a partial self substitute
            class _FakeCoord:
                _chart_log = chart_log_stub
                learning = MagicMock()

            fc = _FakeCoord()

            with caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.coordinator"):
                coord_mod.ClimateAdvisorCoordinator._run_solar_phase_chart_log_fit(fc, backfill=False)

        # The method currently returns silently when no windows found.
        # After our fix it must log an INFO line mentioning the entry count and 0 windows.
        solar_logs = [r for r in caplog.records if "solar phase fit" in r.message.lower()]
        assert solar_logs, (
            "Expected at least one INFO log line containing 'Solar phase fit' when no windows found. "
            f"All logged lines: {[r.message for r in caplog.records]}"
        )

    def test_solar_phase_fit_logs_ewma_update(self, caplog):
        """When a qualifying window is committed via EWMA, a log line with 'Solar phase EWMA'
        must be emitted with old/new values."""
        from datetime import datetime

        from custom_components.climate_advisor import coordinator as coord_mod

        # Entries that will form a daytime passive window (hour=10, regime=off/off/closed).
        # Use June 12 (same as fixed_now) so the cutoff check (last 2 days) passes.
        daytime_entries = [
            _make_chart_log_entry(f"2026-06-12T{h:02d}:00:00+00:00", indoor=72.0 + h * 0.2, outdoor=65.0)
            for h in range(10, 16)  # 6 entries at hour 10–15, all daytime
        ]

        chart_log_stub = MagicMock()
        chart_log_stub._entries = daytime_entries

        fixed_now = datetime(2026, 6, 12, 18, 0, 0, tzinfo=UTC)

        def _as_local(dt):
            return dt  # UTC hours 10–15 are daytime

        with (
            patch.object(coord_mod, "dt_util") as mock_dt,
            patch(
                "custom_components.climate_advisor.coordinator._estimate_solar_phase_offset",
                return_value=(12.5, None),  # successful fit, obs_h=12.5
            ),
        ):
            mock_dt.now.return_value = fixed_now
            mock_dt.as_local.side_effect = _as_local

            class _FakeCoord:
                _chart_log = chart_log_stub
                learning = MagicMock()

            fc = _FakeCoord()

            with caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.coordinator"):
                coord_mod.ClimateAdvisorCoordinator._run_solar_phase_chart_log_fit(fc, backfill=False)

        ewma_logs = [r for r in caplog.records if "solar phase ewma" in r.message.lower()]
        assert ewma_logs, (
            "Expected an INFO log line containing 'Solar phase EWMA' after a successful phase fit commit. "
            f"All logged lines: {[r.message for r in caplog.records]}"
        )
