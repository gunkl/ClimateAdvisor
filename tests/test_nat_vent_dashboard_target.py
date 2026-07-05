"""Tests for nat-vent dashboard target sleep-window alignment (Issue #400).

Issue #374 (v0.4.47) fixed the fan's actual cycling target in
automation.py::nat_vent_temperature_check() to use sleep_heat + hysteresis during the
sleep window instead of the daytime comfort-band midpoint. That fix never touched
coordinator.py::get_debug_state(), which independently (and incorrectly) always
computed the daytime midpoint for the nat_vent_target/on_threshold/off_threshold
fields exposed to the dashboard — so during the sleep window the dashboard showed a
target (e.g. 71°F) that did not match what the fan was actually doing (e.g. 66°F).

Occupant impact: the fan is behaving correctly, but the status page misleads the
user/developer into thinking nat-vent is still using the daytime target, making it
impossible to verify the #374 fix from the UI.
"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

_THERMOSTAT_ID = "climate.thermostat"
_PATCH_DT_NOW = "custom_components.climate_advisor.coordinator.dt_util.now"


def _get_coordinator_class():
    """Return the current ClimateAdvisorCoordinator class — avoids stale __globals__."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _make_nat_vent_coord_stub(*, config: dict) -> object:
    """Build a minimal coordinator stub sufficient to call the real get_debug_state()."""
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = MagicMock()
    coord.config = config
    coord.data = {}
    coord._resolved_sensors = []
    coord._door_open_timers = {}
    coord._current_classification = None
    coord._automation_enabled = True
    coord._occupancy_mode = "home"
    coord._occupancy_away_timer_cancel = None
    coord._startup_coalesce_active = False
    coord._startup_coalesce_expiry = None
    coord._build_thermal_pipeline_summary = MagicMock(return_value={})

    ae = MagicMock()
    ae._natural_vent_active = True
    ae._fan_active = True
    ae._fan_override_active = False
    ae._manual_override_active = False
    ae._grace_active = False
    ae._grace_end_time = None
    ae.is_paused_by_door = False
    ae._last_classification_applied = None
    ae._pre_pause_mode = None
    ae._last_resume_source = None
    ae.config = config
    coord.automation_engine = ae

    return coord


class TestNatVentDashboardTargetSleepWindow:
    """get_debug_state() must match automation.py's sleep-vs-daytime nat-vent target."""

    def test_daytime_target_is_comfort_midpoint(self):
        """Outside the sleep window, target stays the comfort-band midpoint (regression guard)."""
        config = {
            "comfort_heat": 68,
            "comfort_cool": 74,
            "sleep_heat": 65,
            "sleep_time": "22:00",
            "wake_time": "07:00",
        }
        coord = _make_nat_vent_coord_stub(config=config)

        # 14:00 — outside the 22:00-07:00 sleep window
        with patch(_PATCH_DT_NOW, return_value=datetime(2026, 7, 2, 14, 0, 0)):
            state = coord.get_debug_state()

        assert state["nat_vent_target"] == 71.0  # (68 + 74) / 2
        assert state["nat_vent_on_threshold"] == 72.0
        assert state["nat_vent_off_threshold"] == 70.0

    def test_sleep_window_target_uses_sleep_heat_not_daytime_midpoint(self):
        """During the sleep window, target must follow sleep_heat + hysteresis (Issue #374 parity).

        Before the fix, this always returned the daytime midpoint (71°F) even overnight,
        contradicting the fan's actual cycling target from automation.py.
        """
        config = {
            "comfort_heat": 68,
            "comfort_cool": 74,
            "sleep_heat": 65,
            "sleep_time": "22:00",
            "wake_time": "07:00",
        }
        coord = _make_nat_vent_coord_stub(config=config)

        # 02:00 — inside the overnight sleep window
        with patch(_PATCH_DT_NOW, return_value=datetime(2026, 7, 2, 2, 0, 0)):
            state = coord.get_debug_state()

        assert state["nat_vent_target"] == 66.0  # sleep_heat(65) + hysteresis(1)
        assert state["nat_vent_target"] != 71.0
        assert state["nat_vent_on_threshold"] == 67.0
        assert state["nat_vent_off_threshold"] == 65.0

    def test_target_is_none_when_nat_vent_inactive(self):
        """No active nat-vent session → all target/threshold fields are None."""
        config = {
            "comfort_heat": 68,
            "comfort_cool": 74,
            "sleep_heat": 65,
            "sleep_time": "22:00",
            "wake_time": "07:00",
        }
        coord = _make_nat_vent_coord_stub(config=config)
        coord.automation_engine._natural_vent_active = False

        with patch(_PATCH_DT_NOW, return_value=datetime(2026, 7, 2, 2, 0, 0)):
            state = coord.get_debug_state()

        assert state["nat_vent_target"] is None
        assert state["nat_vent_on_threshold"] is None
        assert state["nat_vent_off_threshold"] is None


class TestComputeNatVentCyclingBand:
    """Issue #402 follow-up: compute_nat_vent_cycling_band() is the extracted single

    source of truth get_debug_state() now delegates to — this class locks in that the
    extraction is behavior-preserving and covers the method directly (rather than only
    indirectly through get_debug_state()), since it's now also called from api.py's main
    status endpoint to power the Natural Vent status card's cycling-band display.
    """

    def _config(self):
        return {
            "comfort_heat": 68,
            "comfort_cool": 74,
            "sleep_heat": 65,
            "sleep_time": "22:00",
            "wake_time": "07:00",
        }

    def test_daytime_band(self):
        coord = _make_nat_vent_coord_stub(config=self._config())
        with patch(_PATCH_DT_NOW, return_value=datetime(2026, 7, 2, 14, 0, 0)):
            band = coord.compute_nat_vent_cycling_band()
        assert band == {"nat_vent_target": 71.0, "nat_vent_on_threshold": 72.0, "nat_vent_off_threshold": 70.0}

    def test_sleep_window_band(self):
        coord = _make_nat_vent_coord_stub(config=self._config())
        with patch(_PATCH_DT_NOW, return_value=datetime(2026, 7, 2, 2, 0, 0)):
            band = coord.compute_nat_vent_cycling_band()
        assert band == {"nat_vent_target": 66.0, "nat_vent_on_threshold": 67.0, "nat_vent_off_threshold": 65.0}

    def test_none_when_inactive(self):
        coord = _make_nat_vent_coord_stub(config=self._config())
        coord.automation_engine._natural_vent_active = False
        with patch(_PATCH_DT_NOW, return_value=datetime(2026, 7, 2, 2, 0, 0)):
            band = coord.compute_nat_vent_cycling_band()
        assert band == {"nat_vent_target": None, "nat_vent_on_threshold": None, "nat_vent_off_threshold": None}

    def test_get_debug_state_and_direct_call_agree(self):
        """get_debug_state() must delegate to compute_nat_vent_cycling_band(), not

        reimplement it separately — regression guard against the extraction drifting
        back into two copies.
        """
        coord = _make_nat_vent_coord_stub(config=self._config())
        with patch(_PATCH_DT_NOW, return_value=datetime(2026, 7, 2, 2, 0, 0)):
            band = coord.compute_nat_vent_cycling_band()
            state = coord.get_debug_state()
        assert state["nat_vent_target"] == band["nat_vent_target"]
        assert state["nat_vent_on_threshold"] == band["nat_vent_on_threshold"]
        assert state["nat_vent_off_threshold"] == band["nat_vent_off_threshold"]


class TestComputeAutomationStatusNatVentTarget:
    """Issue #415: _compute_automation_status()'s nat-vent branch must never embed a

    numeric target. It is cached for up to update_interval (30 min) while api.py
    recomputes compute_nat_vent_cycling_band() live on every dashboard poll for the
    cycling-band line — so a number embedded here can silently drift from the live
    band across a sleep-window boundary (e.g. cached "71°F" vs. live "64°F–66°F").
    This repeated the "fix one duplicate implementation, miss the sibling" pattern
    from #374/#400/#402 because every prior fix (#407, #409) corrected which formula
    to use rather than removing the second, independently-timed computation.

    Issue #409: the "windows open · " prefix was dropped — natural_vent_active does
    not imply a sensor is open, and real window state is already shown by the
    dedicated Doors/Windows status card, so restating it here was both potentially
    inaccurate and duplicative.
    """

    def _config(self):
        return {
            "comfort_heat": 68,
            "comfort_cool": 74,
            "sleep_heat": 65,
            "sleep_time": "22:00",
            "wake_time": "07:00",
        }

    def test_daytime_status_has_no_numeric_target(self):
        """Outside the sleep window, the status string is plain "nat-vent"."""
        coord = _make_nat_vent_coord_stub(config=self._config())

        # 14:00 — outside the 22:00-07:00 sleep window
        with patch(_PATCH_DT_NOW, return_value=datetime(2026, 7, 2, 14, 0, 0)):
            status = coord._compute_automation_status()

        assert status == "nat-vent"

    def test_sleep_window_status_has_no_numeric_target(self):
        """During the sleep window, the status string is also plain "nat-vent" —

        no number is embedded here at all, so it can never disagree with the live
        cycling-band line regardless of which side of the sleep-window boundary the
        coordinator's cached status was last computed on.
        """
        coord = _make_nat_vent_coord_stub(config=self._config())

        # 02:00 — inside the overnight sleep window
        with patch(_PATCH_DT_NOW, return_value=datetime(2026, 7, 2, 2, 0, 0)):
            status = coord._compute_automation_status()

        assert status == "nat-vent"
        assert "71" not in status
        assert "°F" not in status

    def test_stale_cached_status_cannot_disagree_with_live_band(self):
        """Regression guard for the recurring bug: simulate a coordinator whose

        automation_status was cached during the daytime (stale, up to 30 min old)
        while compute_nat_vent_cycling_band() is queried live during the sleep
        window (as api.py does on every poll). Since automation_status embeds no
        number, the two can never visibly disagree, no matter how stale the cache is.
        """
        coord = _make_nat_vent_coord_stub(config=self._config())

        # Cache automation_status as of 21:59 (still daytime).
        with patch(_PATCH_DT_NOW, return_value=datetime(2026, 7, 2, 21, 59, 0)):
            cached_status = coord._compute_automation_status()

        # api.py queries the live band 2 minutes later, now inside the sleep window.
        with patch(_PATCH_DT_NOW, return_value=datetime(2026, 7, 2, 22, 1, 0)):
            live_band = coord.compute_nat_vent_cycling_band()

        assert cached_status == "nat-vent"
        assert f"{live_band['nat_vent_target']:.0f}" not in cached_status

    def test_status_does_not_claim_windows_open(self):
        """Issue #409: natural_vent_active does not imply a window/door sensor is open

        (it can activate purely on temperature/idle-HVAC conditions, and contact sensors
        are optional config), so the nat-vent status string must not assert "windows open"
        — that fact belongs solely to the dedicated Doors/Windows status card.
        """
        coord = _make_nat_vent_coord_stub(config=self._config())

        with patch(_PATCH_DT_NOW, return_value=datetime(2026, 7, 2, 14, 0, 0)):
            status = coord._compute_automation_status()

        assert "windows open" not in status
