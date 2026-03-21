"""Tests for conditional startup manual override (Issue #42).

Verifies that on first run, manual override is only set when the current
HVAC mode differs from the classification's recommended mode.  When modes
match, no override is needed — Climate Advisor already agrees with the
current state.

See: GitHub Issue #42
"""

from __future__ import annotations

import sys
from datetime import datetime
from unittest.mock import MagicMock

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# Patch dt_util.now to return a real datetime (needed for isoformat() calls)
sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 3, 20, 7, 0, 0)

from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_classification(**overrides):
    """Build a DayClassification bypassing __post_init__."""
    c = object.__new__(DayClassification)
    defaults = {
        "day_type": "warm",
        "trend_direction": "stable",
        "trend_magnitude": 0,
        "today_high": 78,
        "today_low": 58,
        "tomorrow_high": 79,
        "tomorrow_low": 59,
        "hvac_mode": "off",
        "pre_condition": False,
        "pre_condition_target": None,
        "windows_recommended": False,
        "window_open_time": None,
        "window_close_time": None,
        "setback_modifier": 0.0,
    }
    defaults.update(overrides)
    c.__dict__.update(defaults)
    return c


def _make_climate_state(state: str):
    """Create a mock HA state object with the given state string."""
    mock = MagicMock()
    mock.state = state
    return mock


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    coro.close()


def _make_coordinator():
    """Create a minimal ClimateAdvisorCoordinator for testing _check_startup_override."""
    hass = MagicMock()
    hass.services.async_call = MagicMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    config = {
        "climate_entity": "climate.thermostat",
        "weather_entity": "weather.forecast_home",
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
    }

    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = hass
    coord.config = config

    # Create a minimal automation engine mock with override fields
    ae = MagicMock()
    ae._manual_override_active = False
    ae._manual_override_mode = None
    ae._manual_override_time = None
    coord.automation_engine = ae

    return coord


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStartupOverride:
    """Verify that first-run override is only set when HVAC mode mismatches."""

    def test_hvac_matches_classification_no_override(self):
        """HVAC is 'heat' and classification recommends 'heat' — no override."""
        coord = _make_coordinator()
        classification = _make_classification(day_type="cold", hvac_mode="heat")
        climate_state = _make_climate_state("heat")

        result = coord._check_startup_override(climate_state, classification)

        assert result is False
        assert coord.automation_engine._manual_override_active is False

    def test_hvac_mismatches_classification_sets_override(self):
        """HVAC is 'heat' but classification recommends 'cool' — override set."""
        coord = _make_coordinator()
        classification = _make_classification(day_type="hot", hvac_mode="cool")
        climate_state = _make_climate_state("heat")

        result = coord._check_startup_override(climate_state, classification)

        assert result is True
        assert coord.automation_engine._manual_override_active is True
        assert coord.automation_engine._manual_override_mode == "heat"
        assert coord.automation_engine._manual_override_time is not None

    def test_hvac_off_no_override(self):
        """HVAC is 'off' — no override regardless of classification."""
        coord = _make_coordinator()
        classification = _make_classification(day_type="hot", hvac_mode="cool")
        climate_state = _make_climate_state("off")

        result = coord._check_startup_override(climate_state, classification)

        assert result is False
        assert coord.automation_engine._manual_override_active is False

    def test_hvac_active_classification_off_sets_override(self):
        """HVAC is 'heat' but classification says 'off' — override set."""
        coord = _make_coordinator()
        classification = _make_classification(day_type="mild", hvac_mode="off")
        climate_state = _make_climate_state("heat")

        result = coord._check_startup_override(climate_state, classification)

        assert result is True
        assert coord.automation_engine._manual_override_active is True
        assert coord.automation_engine._manual_override_mode == "heat"

    def test_hvac_cool_classification_heat_sets_override(self):
        """HVAC is 'cool' but classification recommends 'heat' — override set."""
        coord = _make_coordinator()
        classification = _make_classification(day_type="cold", hvac_mode="heat")
        climate_state = _make_climate_state("cool")

        result = coord._check_startup_override(climate_state, classification)

        assert result is True
        assert coord.automation_engine._manual_override_active is True
        assert coord.automation_engine._manual_override_mode == "cool"

    def test_hvac_unavailable_no_override(self):
        """HVAC is 'unavailable' — no override set."""
        coord = _make_coordinator()
        classification = _make_classification(day_type="hot", hvac_mode="cool")
        climate_state = _make_climate_state("unavailable")

        result = coord._check_startup_override(climate_state, classification)

        assert result is False
        assert coord.automation_engine._manual_override_active is False

    def test_no_climate_state_no_override(self):
        """Climate state is None (entity not yet available) — no override."""
        coord = _make_coordinator()
        classification = _make_classification(day_type="hot", hvac_mode="cool")

        result = coord._check_startup_override(None, classification)

        assert result is False
        assert coord.automation_engine._manual_override_active is False

    def test_hvac_unknown_no_override(self):
        """HVAC is 'unknown' — no override set."""
        coord = _make_coordinator()
        classification = _make_classification(day_type="cold", hvac_mode="heat")
        climate_state = _make_climate_state("unknown")

        result = coord._check_startup_override(climate_state, classification)

        assert result is False
        assert coord.automation_engine._manual_override_active is False
