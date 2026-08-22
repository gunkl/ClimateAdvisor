"""Tests for the hard-invariant watchdog (Issue #749).

Reproduction context: the 2026-08-22 live incident (#739/#748) had the AC and the
whole-house fan running simultaneously for 5+ minutes while CA's own internal bookkeeping
(_fan_override_active, _fan_remote_timer_hours) stayed entirely self-consistent. This module
exists to catch that class of bug by reading ground truth directly, independent of any
internal flag state.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.const import (  # noqa: E402
    FAN_MODE_BOTH,
    FAN_MODE_DISABLED,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
)
from custom_components.climate_advisor.invariant_watchdog import (  # noqa: E402
    InvariantViolation,
    check_ac_whf_mutex,
    run_invariant_checks,
)

# ---------------------------------------------------------------------------
# check_ac_whf_mutex — pure function
# ---------------------------------------------------------------------------


class TestCheckAcWhfMutex:
    def test_both_off_no_violation(self):
        assert check_ac_whf_mutex(hvac_action="idle", whf_physically_on=False, fan_mode=FAN_MODE_WHOLE_HOUSE) is None

    def test_ac_on_whf_off_no_violation(self):
        assert check_ac_whf_mutex(hvac_action="cooling", whf_physically_on=False, fan_mode=FAN_MODE_WHOLE_HOUSE) is None

    def test_ac_off_whf_on_no_violation(self):
        assert check_ac_whf_mutex(hvac_action="idle", whf_physically_on=True, fan_mode=FAN_MODE_WHOLE_HOUSE) is None

    def test_ac_cooling_whf_on_is_violation(self):
        result = check_ac_whf_mutex(hvac_action="cooling", whf_physically_on=True, fan_mode=FAN_MODE_WHOLE_HOUSE)
        assert isinstance(result, InvariantViolation)
        assert result.name == "ac_whf_mutex"
        assert "cooling" in result.detail

    def test_ac_heating_whf_on_is_violation(self):
        result = check_ac_whf_mutex(hvac_action="heating", whf_physically_on=True, fan_mode=FAN_MODE_BOTH)
        assert isinstance(result, InvariantViolation)

    def test_ac_fan_only_whf_on_no_violation(self):
        """hvac_action='fan' (blower running, compressor idle) is not an active mode."""
        assert check_ac_whf_mutex(hvac_action="fan", whf_physically_on=True, fan_mode=FAN_MODE_WHOLE_HOUSE) is None

    def test_fan_mode_hvac_archetype_never_violates(self):
        """FAN_MODE_HVAC coexists with the compressor by design — that fan IS the
        thermostat's own blower, not a whole-house fan fighting it."""
        assert check_ac_whf_mutex(hvac_action="cooling", whf_physically_on=True, fan_mode=FAN_MODE_HVAC) is None

    def test_fan_mode_disabled_never_violates(self):
        assert check_ac_whf_mutex(hvac_action="cooling", whf_physically_on=True, fan_mode=FAN_MODE_DISABLED) is None

    def test_unknown_whf_physical_state_no_violation(self):
        """whf_physically_on=None (feedback not configured/available) — cannot confirm a
        violation without a physical read, so no violation is reported rather than guessing."""
        assert check_ac_whf_mutex(hvac_action="cooling", whf_physically_on=None, fan_mode=FAN_MODE_WHOLE_HOUSE) is None

    def test_unknown_hvac_action_no_violation(self):
        assert check_ac_whf_mutex(hvac_action=None, whf_physically_on=True, fan_mode=FAN_MODE_WHOLE_HOUSE) is None


class TestRunInvariantChecks:
    def test_returns_empty_list_when_nothing_violated(self):
        assert run_invariant_checks(hvac_action="idle", whf_physically_on=False, fan_mode=FAN_MODE_WHOLE_HOUSE) == []

    def test_returns_violation_list(self):
        violations = run_invariant_checks(hvac_action="cooling", whf_physically_on=True, fan_mode=FAN_MODE_WHOLE_HOUSE)
        assert len(violations) == 1
        assert violations[0].name == "ac_whf_mutex"


# ---------------------------------------------------------------------------
# Coordinator wiring — real ClimateAdvisorCoordinator._run_invariant_watchdog()
# ---------------------------------------------------------------------------


def _make_real_coordinator(*, whf_physically_on, fan_mode, is_duplicate: bool = False):
    """Build a bare ClimateAdvisorCoordinator bound to the real
    _run_invariant_watchdog method (object.__new__() + types.MethodType(), the
    established partial-instantiation pattern — see test_contact_status.py).
    """
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.config = {"fan_mode": fan_mode}
    coord.hass = MagicMock()
    coord.hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    coord._get_fan_physical_state = MagicMock(return_value=whf_physically_on)
    coord._emit_event = MagicMock()
    coord.automation_engine = MagicMock()
    coord.automation_engine._recent_duplicate = MagicMock(return_value=is_duplicate)
    coord.automation_engine._notify = MagicMock(side_effect=lambda *a, **kw: _noop())
    coord._run_invariant_watchdog = types.MethodType(ClimateAdvisorCoordinator._run_invariant_watchdog, coord)
    return coord


async def _noop():
    return None


class TestCoordinatorInvariantWatchdogWiring:
    def test_no_violation_emits_nothing(self):
        coord = _make_real_coordinator(whf_physically_on=False, fan_mode=FAN_MODE_WHOLE_HOUSE)

        result = coord._run_invariant_watchdog(hvac_action="idle")

        assert result == []
        coord._emit_event.assert_not_called()
        coord.automation_engine._notify.assert_not_called()
        coord.hass.async_create_task.assert_not_called()

    def test_violation_emits_event_and_notifies(self):
        coord = _make_real_coordinator(whf_physically_on=True, fan_mode=FAN_MODE_WHOLE_HOUSE)

        result = coord._run_invariant_watchdog(hvac_action="cooling")

        assert len(result) == 1
        assert result[0].name == "ac_whf_mutex"
        coord._emit_event.assert_called_once()
        event_type, event_data = coord._emit_event.call_args.args
        assert event_type == "invariant_violation"
        assert event_data["invariant"] == "ac_whf_mutex"
        coord.hass.async_create_task.assert_called_once()

    def test_duplicate_violation_within_window_does_not_re_notify(self):
        """Issue #749: a violation that persists across coordinator ticks (e.g. the
        underlying bug isn't fixed yet) must not spam a fresh push notification every
        cycle — dedup mirrors the existing state_contradiction_warning pattern."""
        coord = _make_real_coordinator(whf_physically_on=True, fan_mode=FAN_MODE_WHOLE_HOUSE, is_duplicate=True)

        result = coord._run_invariant_watchdog(hvac_action="cooling")

        assert len(result) == 1, "The violation itself is still returned/logged every cycle"
        coord._emit_event.assert_not_called()
        coord.automation_engine._notify.assert_not_called()
