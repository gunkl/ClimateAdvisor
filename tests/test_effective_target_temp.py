"""Tests for the always-populated "effective target temperature" (Issue #998).

Tests cover the 3-tier priority in ClimateAdvisorCoordinator._compute_effective_target_now():
1. HVAC actively driving -> the thermostat's own setpoint
2. A CA-relevant fan mechanism is physically running (ground-truth aware, including a
   manual override -- the case Issue #993's nat-vent-only sensor misses) -> the
   nat-vent-style cycling target
3. Otherwise -> the passive comfort-band edge
"""

from __future__ import annotations

import sys
import types
from datetime import datetime
from unittest.mock import MagicMock

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 7, 15, 14, 0, 0)

from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402


def _make_coordinator(config: dict, *, day_type: str = "warm") -> object:
    """Build a bare ClimateAdvisorCoordinator bound to the real effective-target methods.

    Uses object.__new__() + types.MethodType() (the established partial-instantiation
    pattern — see test_contact_status.py's _make_real_coordinator) rather than
    replicating the method bodies, so these tests exercise the real
    ClimateAdvisorCoordinator._compute_effective_target_now()/_fan_cycling_target_now()/
    _nat_vent_target_now()/_resolve_active_comfort_band().
    """
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.config = config
    coord.automation_engine = MagicMock()
    coord.automation_engine._occupancy_mode = "home"
    coord.automation_engine._natural_vent_active = False
    coord._current_classification = DayClassification(
        day_type=day_type,
        trend_direction="stable",
        trend_magnitude=0.0,
        today_high=80.0,
        today_low=65.0,
        tomorrow_high=80.0,
        tomorrow_low=65.0,
    )
    coord._compute_effective_target_now = types.MethodType(
        ClimateAdvisorCoordinator._compute_effective_target_now, coord
    )
    coord._fan_cycling_target_now = types.MethodType(ClimateAdvisorCoordinator._fan_cycling_target_now, coord)
    coord._nat_vent_target_now = types.MethodType(ClimateAdvisorCoordinator._nat_vent_target_now, coord)
    coord._resolve_active_comfort_band = types.MethodType(ClimateAdvisorCoordinator._resolve_active_comfort_band, coord)
    return coord


_BASE_CONFIG = {"comfort_heat": 68.0, "comfort_cool": 74.0}


class TestHvacTierTakesPriority:
    def test_hvac_active_mode_with_setpoint_wins(self):
        coord = _make_coordinator(_BASE_CONFIG)
        value, source = coord._compute_effective_target_now(hvac_mode="cool", target_temp=72.0, fan_status="active")
        assert (value, source) == (72.0, "hvac")

    def test_hvac_off_falls_through_even_with_setpoint_present(self):
        coord = _make_coordinator(_BASE_CONFIG)
        value, source = coord._compute_effective_target_now(hvac_mode="off", target_temp=72.0, fan_status="inactive")
        assert source != "hvac"

    def test_hvac_active_mode_but_no_setpoint_falls_through(self):
        """A real commanded setpoint must be present, not just an active mode string —
        guards against reporting a stale/missing value as if HVAC were driving."""
        coord = _make_coordinator(_BASE_CONFIG)
        value, source = coord._compute_effective_target_now(hvac_mode="cool", target_temp=None, fan_status="active")
        assert source != "hvac"


class TestWhfTierGroundTruthAware:
    def test_active_fan_status_uses_cycling_target(self):
        coord = _make_coordinator(_BASE_CONFIG)
        value, source = coord._compute_effective_target_now(hvac_mode="off", target_temp=None, fan_status="active")
        assert source == "whf"
        assert value == 71.0  # daytime midpoint of comfort_heat=68/comfort_cool=74

    def test_manual_override_running_uses_cycling_target(self):
        """Issue #998's whole point: a manual RF-remote override (Issue #993's
        nat-vent-only sensor goes unavailable here) must still resolve to the
        cycling target, not fall through to the passive tier."""
        coord = _make_coordinator(_BASE_CONFIG)
        value, source = coord._compute_effective_target_now(
            hvac_mode="off", target_temp=None, fan_status="running (manual override)"
        )
        assert source == "whf"
        assert value == 71.0

    def test_running_untracked_uses_cycling_target(self):
        coord = _make_coordinator(_BASE_CONFIG)
        value, source = coord._compute_effective_target_now(
            hvac_mode="off", target_temp=None, fan_status="running (untracked)"
        )
        assert source == "whf"

    def test_active_unconfirmed_uses_cycling_target(self):
        coord = _make_coordinator(_BASE_CONFIG)
        value, source = coord._compute_effective_target_now(
            hvac_mode="off", target_temp=None, fan_status="active (unconfirmed)"
        )
        assert source == "whf"

    def test_inactive_status_does_not_use_whf_tier(self):
        coord = _make_coordinator(_BASE_CONFIG)
        value, source = coord._compute_effective_target_now(hvac_mode="off", target_temp=None, fan_status="inactive")
        assert source != "whf"

    def test_disabled_status_does_not_use_whf_tier(self):
        coord = _make_coordinator(_BASE_CONFIG)
        value, source = coord._compute_effective_target_now(hvac_mode="off", target_temp=None, fan_status="disabled")
        assert source != "whf"


class TestPassiveTierFallback:
    def test_nothing_running_uses_comfort_ceiling_on_warm_day(self):
        coord = _make_coordinator(_BASE_CONFIG, day_type="warm")
        value, source = coord._compute_effective_target_now(hvac_mode="off", target_temp=None, fan_status="inactive")
        assert source == "passive"
        assert value == 74.0  # comfort_cool ceiling — a warm day's classified hvac_mode is "off", not "heat"

    def test_nothing_running_uses_comfort_floor_on_cool_day(self):
        coord = _make_coordinator(_BASE_CONFIG, day_type="cool")
        value, source = coord._compute_effective_target_now(hvac_mode="off", target_temp=None, fan_status="inactive")
        assert source == "passive"
        assert value == 68.0  # comfort_heat floor — a "cool" day classifies hvac_mode="heat"

    def test_always_returns_a_value_even_with_nothing_active(self):
        """The core Issue #998 requirement: never None/unavailable during normal
        operation, regardless of which mechanism (or none) is currently active."""
        coord = _make_coordinator(_BASE_CONFIG)
        value, _source = coord._compute_effective_target_now(hvac_mode="off", target_temp=None, fan_status="inactive")
        assert value is not None
