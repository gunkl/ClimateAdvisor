"""Tests for the comfort-family switch lockout (Issue #821 Design §4), RETIRED by
Issue #827 — see that issue's history below, added to the #821/#823 history this
docstring already tracked.

Verification (Opus, independent review) found this mechanism provably decorative:
patching `_family_switch_locked_out()` to always `return False` and rerunning the full
suite produced zero failures — nothing detected its removal. This file provided real
coverage: direct unit tests for `_family_switch_locked_out()`/`_arm_comfort_family()`,
plus a genuine negative-control integration test that reproduced real heat/cool-family
hunting when the lockout was removed and proved the lockout prevented it — the exact
"revert-test each fix" standard from CLAUDE.md. Every test in the negative-control
class was verified (by the Executor, manually, before considering that done) to
actually FAIL when `_family_switch_locked_out()` was patched to always return False.

Issue #827 (this consolidation): the three-authority split this file's own module
docstring already described as the root cause of #821/#823's recurring bugs
(`select_comfort_band()`'s day-type edge picker, the confidence-gated fallback
resolver, and this file's own dwell-timer lockout) has been retired into a single
FSM (`comfort_family_fsm.py`'s `transition()`, wired via `comfort_family_decision.py`).
`_resolve_comfort_family_mode()` and `_family_switch_locked_out()` are DELETED —
min-dwell anti-flap now lives exclusively inside `comfort_family_fsm.transition()`,
which only advances its `dwell_since` clock on a genuine state change, never on mere
reassertion. This makes the #823 failure class (a reassertion resetting a clock that
should only move on real change) structurally unrepresentable rather than patched
around — see `comfort_family_fsm.py`'s own module docstring for the full mechanism.

What moved where:
- `TestFamilySwitchLockedOut` (direct `_family_switch_locked_out()` unit tests,
  cold-start/same-family/window-boundary/configurable-interval cases) → superseded by
  `tests/test_comfort_family_fsm.py::TestColdStart` and `::TestMinDwellAntiFlap`, which
  test the equivalent guarantees (cold start never locked out, reassertion never resets
  the dwell clock, locked-out-within-window / unlocked-once-elapsed) against the real
  FSM `transition()` entry point instead of the retired method.
- `TestFamilySwitchLockoutPreventsHunting` (the family-flap negative-control
  integration test) → superseded by `tests/test_comfort_family_decision.py::
  TestRecoveryMarginHysteresis` (the mechanism that now prevents this class of
  hunting — `test_instant_de_escalation_does_not_happen_at_bare_threshold` is the
  direct equivalent negative control) plus `TestAgainstGrainDeadbandHeld`/
  `TestAgainstGrainDeadbandCleared` (the deadband gate that also contributes to
  hunting prevention on the entry side). The old dwell-timer lockout guarded the
  ENTRY direction only and did nothing for exit — the FSM's recovery-margin
  hysteresis on REVERT is what actually closes the saw-tooth gap the old mechanism
  never addressed; see `tools/simulations/pending/
  issue_827_recovery_margin_prevents_sawtooth.json` for the real-engine, end-to-end
  proof of the same guarantee.
- `TestPermanentLockoutFixIssue823` (the live-incident reassertion-cadence
  regression test and its own negative control) → superseded by
  `tests/test_comfort_family_fsm.py::TestMinDwellAntiFlap::
  test_what_would_happen_if_dwell_reset_on_every_call_regression_control` — the
  direct #823-regression-class negative control against the real FSM, proving the
  bug class is now structurally unrepresentable (only one place the dwell clock can
  move, and it only moves on a genuine transition) rather than patched around by a
  reassertion-aware `only_if_changed` flag.

`TestArmComfortFamily`/`TestArmComfortFamilyOnlyIfChanged` below are UNCHANGED and
still pass as-is — `_arm_comfort_family()` survives Issue #827, narrowed to a thin
compatibility writer (Design §2): the FSM's own wiring calls it once per cycle to
keep `self._comfort_mode_family` populated for `tools/sim_harness/outcomes.py`'s
`"comfort_family"` assertion type, and the 7 out-of-scope callers (nat-vent/WHF
activation, `_exit_nat_vent()`, etc.) keep calling it directly for their own
bookkeeping, unchanged by this issue.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.const import CLIMATE_FEATURE_TARGET_TEMP_RANGE

_T0 = datetime(2026, 1, 1, 12, 0, 0)


def _consume_coroutine(coro):
    coro.close()


def _make_engine(
    *, indoor_temp: float, comfort_heat: float = 68.0, comfort_cool: float = 76.0, **cfg
) -> AutomationEngine:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    attrs = {
        "hvac_modes": ["off", "heat", "cool"],
        "supported_features": CLIMATE_FEATURE_TARGET_TEMP_RANGE,
        "current_temperature": indoor_temp,
    }
    climate_state = MagicMock()
    climate_state.state = "cool"
    climate_state.attributes = attrs
    hass.states.get.return_value = climate_state

    config = {
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "setback_heat": 60.0,
        "setback_cool": 82.0,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
        **cfg,
    }

    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service=config["notify_service"],
        config=config,
    )


# ---------------------------------------------------------------------------
# TestFamilySwitchLockedOut — RETIRED by Issue #827. `_family_switch_locked_out()`
# is deleted; min-dwell anti-flap now lives in `comfort_family_fsm.transition()`.
# Equivalent coverage: tests/test_comfort_family_fsm.py::TestColdStart (cold-start-
# never-locked-out) and ::TestMinDwellAntiFlap (reassertion never resets the dwell
# clock, locked-out-within-window, unlocked-once-elapsed, configurable interval —
# all against the real transition() entry point). See this file's module docstring
# for the full "what moved where" mapping.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Direct unit tests: _arm_comfort_family()
# ---------------------------------------------------------------------------


class TestArmComfortFamily:
    """Default path (``only_if_changed=False``, the implicit default). This is the
    correct, already-verified behavior for the 9 genuine one-shot transition-event
    call sites (nat-vent/WHF activation, ``_exit_nat_vent()`` and its bypass
    branches) — each fires exactly once per real physical event, so "always reset"
    is intentional here. See ``TestArmComfortFamilyOnlyIfChanged`` below for the
    Issue #823 fix covering the 4 steady-state reassertion call sites."""

    def test_first_arm_sets_family_and_entry_time_without_changed_flag(self):
        engine = _make_engine(indoor_temp=70.0)
        assert engine._comfort_mode_family_changed_this_tick is False

        engine._arm_comfort_family("cooling", _T0)

        assert engine._comfort_mode_family == "cooling"
        assert engine._comfort_mode_family_entry_time == _T0
        assert engine._comfort_mode_family_changed_this_tick is False

    def test_reaffirming_same_family_does_not_set_changed_flag(self):
        engine = _make_engine(indoor_temp=70.0)
        engine._arm_comfort_family("cooling", _T0)

        later = _T0 + timedelta(seconds=30)
        engine._arm_comfort_family("cooling", later)

        assert engine._comfort_mode_family_changed_this_tick is False
        assert engine._comfort_mode_family_entry_time == later, "reaffirming resets the dwell clock"

    def test_switching_family_sets_changed_flag(self):
        engine = _make_engine(indoor_temp=70.0)
        engine._arm_comfort_family("cooling", _T0)

        later = _T0 + timedelta(seconds=700)
        engine._arm_comfort_family("heating", later)

        assert engine._comfort_mode_family == "heating"
        assert engine._comfort_mode_family_changed_this_tick is True
        assert engine._comfort_mode_family_entry_time == later


# ---------------------------------------------------------------------------
# Issue #823: only_if_changed=True path — used by _apply_comfort_band() and
# _set_temperature_for_mode(), the 4 call sites that run every classification
# cycle regardless of real state change. Without this, those sites re-arm the
# same family every cycle, permanently pushing the dwell clock forward and
# making the lockout mathematically unreachable-to-clear.
# ---------------------------------------------------------------------------


class TestArmComfortFamilyOnlyIfChanged:
    def test_first_arm_still_sets_family_and_entry_time(self):
        """No prior entry_time recorded — must still arm even with only_if_changed=True,
        matching the default path's cold-start behavior."""
        engine = _make_engine(indoor_temp=70.0)

        engine._arm_comfort_family("cooling", _T0, only_if_changed=True)

        assert engine._comfort_mode_family == "cooling"
        assert engine._comfort_mode_family_entry_time == _T0

    def test_reaffirming_same_family_does_not_reset_entry_time(self):
        """The Issue #823 fix: repeated reassertion of the SAME family must be a true
        no-op for the dwell clock, unlike the default path."""
        engine = _make_engine(indoor_temp=70.0)
        engine._arm_comfort_family("cooling", _T0, only_if_changed=True)

        later = _T0 + timedelta(seconds=300)
        engine._arm_comfort_family("cooling", later, only_if_changed=True)

        assert engine._comfort_mode_family_entry_time == _T0, (
            "reaffirming the same family with only_if_changed=True must NOT reset the "
            "dwell clock — this is the fix for Issue #823's permanent-lockout bug"
        )
        assert engine._comfort_mode_family_changed_this_tick is False

    def test_reaffirming_many_times_never_resets_entry_time(self):
        """Simulates a realistic multi-cycle sequence (classification cadence shorter
        than comfort_mode_switch_min_interval_s) — entry_time must stay pinned to the
        very first arm no matter how many times the same family is reaffirmed."""
        engine = _make_engine(indoor_temp=70.0)
        engine._arm_comfort_family("cooling", _T0, only_if_changed=True)

        for i in range(1, 6):
            engine._arm_comfort_family("cooling", _T0 + timedelta(minutes=5 * i), only_if_changed=True)

        assert engine._comfort_mode_family_entry_time == _T0

    def test_switching_family_still_resets_entry_time_and_sets_changed_flag(self):
        """A genuine transition must still behave exactly like the default path."""
        engine = _make_engine(indoor_temp=70.0)
        engine._arm_comfort_family("cooling", _T0, only_if_changed=True)

        later = _T0 + timedelta(seconds=700)
        engine._arm_comfort_family("heating", later, only_if_changed=True)

        assert engine._comfort_mode_family == "heating"
        assert engine._comfort_mode_family_changed_this_tick is True
        assert engine._comfort_mode_family_entry_time == later


# ---------------------------------------------------------------------------
# TestFamilySwitchLockoutPreventsHunting / TestPermanentLockoutFixIssue823 —
# RETIRED by Issue #827. Both classes drove the real (now-deleted)
# _resolve_comfort_family_mode()/_arm_comfort_family(only_if_changed=True) pair
# through hand-built multi-cycle sequences to prove the dwell-timer lockout
# prevented family hunting and eventually cleared despite a short reassertion
# cadence. The consolidated FSM makes both guarantees structural rather than
# timer-patched:
#
# - Hunting prevention (was TestFamilySwitchLockoutPreventsHunting): now the
#   deadband gate (entry) + recovery-margin hysteresis (exit) in
#   comfort_family_decision.py — see tests/test_comfort_family_decision.py::
#   TestRecoveryMarginHysteresis (test_instant_de_escalation_does_not_happen_at_
#   bare_threshold is the direct negative-control equivalent) and
#   ::TestAgainstGrainDeadbandHeld/::TestAgainstGrainDeadbandCleared. End-to-end,
#   real-engine proof: tools/simulations/pending/
#   issue_827_recovery_margin_prevents_sawtooth.json.
# - Eventually-clears-despite-cadence (was TestPermanentLockoutFixIssue823): now
#   comfort_family_fsm.transition()'s dwell_since only ever advances on a genuine
#   transition (never on reassertion), so the bug class is structurally
#   unrepresentable rather than patched around — see
#   tests/test_comfort_family_fsm.py::TestMinDwellAntiFlap, especially
#   test_what_would_happen_if_dwell_reset_on_every_call_regression_control (the
#   direct #823-regression-class negative control).
#
# See this file's module docstring for the full "what moved where" mapping.
# ---------------------------------------------------------------------------
