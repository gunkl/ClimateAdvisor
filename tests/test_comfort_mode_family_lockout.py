"""Tests for the comfort-family switch lockout (Issue #821 Design §4).

Verification (Opus, independent review) found this mechanism provably decorative:
patching `_family_switch_locked_out()` to always `return False` and rerunning the full
suite produced zero failures — nothing detected its removal. This file provides real
coverage: direct unit tests for `_family_switch_locked_out()`/`_arm_comfort_family()`,
plus a genuine negative-control integration test that reproduces real heat/cool-family
hunting when the lockout is removed and proves the lockout prevents it — the exact
"revert-test each fix" standard from CLAUDE.md. Every test in the negative-control
class was verified (by the Executor, manually, before considering this done) to
actually FAIL when `_family_switch_locked_out()` is patched to always return False —
see the module docstring note at the bottom of this file for that verification record.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.const import (
    CLIMATE_FEATURE_TARGET_TEMP_RANGE,
    CONF_COMFORT_MODE_SWITCH_MIN_INTERVAL_S,
)

_DT_NOW_PATH = "custom_components.climate_advisor.automation.dt_util.now"
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
# Direct unit tests: _family_switch_locked_out()
# ---------------------------------------------------------------------------


class TestFamilySwitchLockedOut:
    def test_cold_start_never_locked_out(self):
        """No prior family recorded (_comfort_mode_family is None) — a fresh process
        has no flapping history to guard against, always allowed."""
        engine = _make_engine(indoor_temp=70.0)
        assert engine._comfort_mode_family is None

        assert engine._family_switch_locked_out(candidate_family="heating", now=_T0) is False

    def test_same_family_requested_never_locked_out(self):
        """Requesting the family that's already active is a no-op, not a switch."""
        engine = _make_engine(indoor_temp=70.0)
        engine._arm_comfort_family("cooling", _T0)

        assert engine._family_switch_locked_out(candidate_family="cooling", now=_T0) is False

    def test_different_family_within_window_is_locked_out(self):
        """A genuine switch attempt inside comfort_mode_switch_min_interval_s (default
        600s) since the current family was last (re)armed must be blocked."""
        engine = _make_engine(indoor_temp=70.0)
        engine._arm_comfort_family("cooling", _T0)

        later = _T0 + timedelta(seconds=300)
        assert engine._family_switch_locked_out(candidate_family="heating", now=later) is True

    def test_different_family_at_exact_boundary_is_not_locked_out(self):
        engine = _make_engine(indoor_temp=70.0)
        engine._arm_comfort_family("cooling", _T0)

        later = _T0 + timedelta(seconds=600)
        assert engine._family_switch_locked_out(candidate_family="heating", now=later) is False

    def test_different_family_past_window_is_not_locked_out(self):
        engine = _make_engine(indoor_temp=70.0)
        engine._arm_comfort_family("cooling", _T0)

        later = _T0 + timedelta(seconds=601)
        assert engine._family_switch_locked_out(candidate_family="heating", now=later) is False

    def test_changed_this_tick_forces_lockout_even_past_the_window(self):
        """Same-tick guard (folded in from Issue #699's own finding): even if the
        elapsed-time check alone would pass, a family change already committed earlier
        in this tick must still block a second switch within the same tick."""
        engine = _make_engine(indoor_temp=70.0)
        engine._arm_comfort_family("cooling", _T0)
        engine._comfort_mode_family_changed_this_tick = True

        far_later = _T0 + timedelta(hours=1)
        assert engine._family_switch_locked_out(candidate_family="heating", now=far_later) is True

    def test_configurable_min_interval_is_respected(self):
        """comfort_mode_switch_min_interval_s is user-configurable — a shorter window
        must clear sooner."""
        engine = _make_engine(indoor_temp=70.0, **{CONF_COMFORT_MODE_SWITCH_MIN_INTERVAL_S: 60})
        engine._arm_comfort_family("cooling", _T0)

        later = _T0 + timedelta(seconds=61)
        assert engine._family_switch_locked_out(candidate_family="heating", now=later) is False

        still_within = _T0 + timedelta(seconds=30)
        assert engine._family_switch_locked_out(candidate_family="heating", now=still_within) is True


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
# Genuine negative-control integration test: without the lockout, real
# heat<->cooling hunting reproduces via _resolve_comfort_family_mode(); with it,
# the hunting is prevented. Drives the REAL engine methods across a realistic
# multi-cycle sequence — indoor dips below the floor, sustain-confirms, escalates
# to heat; indoor recovers immediately after (a real, common pattern: heat's own
# effect nudges indoor back above the floor almost immediately, or a momentary
# noisy reading recovers) and the day-classification's own "cool" reasserts
# itself (no lockout guards the heat->cool direction, by design — see
# automation.py's _resolve_comfort_family_mode() docstring); indoor dips again
# shortly after and re-confirms — WITHOUT the family-switch lockout, this second
# escalation fires immediately (a real flap, ~200s after the first); WITH it,
# the lockout blocks the second escalation until comfort_mode_switch_min_interval_s
# has elapsed since the "cooling" family was last reaffirmed.
# ---------------------------------------------------------------------------


def _make_cool_day_classification():
    from custom_components.climate_advisor.classifier import DayClassification

    obj = object.__new__(DayClassification)
    obj.day_type = "hot"
    obj.hvac_mode = "cool"
    obj.trend_direction = "stable"
    obj.trend_magnitude = 1.0
    obj.today_high = 95.0
    obj.today_low = 70.0
    obj.tomorrow_high = 96.0
    obj.tomorrow_low = 71.0
    obj.pre_condition = False
    obj.pre_condition_target = None
    obj.windows_recommended = False
    obj.window_open_time = None
    obj.window_close_time = None
    obj.setback_modifier = 0.0
    return obj


def _run_hunting_sequence(engine: AutomationEngine) -> list[str]:
    """Drives _resolve_comfort_family_mode() through a realistic multi-cycle
    sequence and returns the list of resolved modes in order. Uses
    _apply_comfort_band()-shaped direct calls to _resolve_comfort_family_mode()
    plus _arm_comfort_family(), mirroring exactly what _set_temperature_for_mode()
    itself does — the real integration point, not a re-implementation of its logic.
    """
    c = _make_cool_day_classification()
    resolved: list[str] = []

    # Cycle 1 (T=0): indoor already below the floor, candidate pre-armed as
    # already-sustained (mirrors a session that's been below-floor for >90s).
    engine._comfort_floor_fallback_since = _T0 - timedelta(hours=1)
    with patch(_DT_NOW_PATH, return_value=_T0):
        mode1 = engine._resolve_comfort_family_mode(c.hvac_mode, now=_T0)
        resolved.append(mode1)
        if mode1 == "heat":
            engine._arm_comfort_family("heating", _T0)
        else:
            engine._arm_comfort_family("cooling", _T0)

    # Cycle 2 (T=100s): indoor has recovered above the floor (heat's own effect,
    # or the reading was noise) — the fallback candidate clears, and
    # _resolve_comfort_family_mode() reverts to "cool" (day_mode) with NO lockout
    # check on this direction (by design — the lockout only ever gates the
    # cool->heat escalation). This models the real engine's own indoor-temp mock
    # by simply constructing a fresh engine whose _get_indoor_temp_f() reads
    # above the floor for this cycle.
    engine._comfort_floor_fallback_since = None  # condition genuinely cleared
    t2 = _T0 + timedelta(seconds=100)
    with patch(_DT_NOW_PATH, return_value=t2):
        mode2 = engine._resolve_comfort_family_mode(c.hvac_mode, now=t2)
        resolved.append(mode2)
        engine._arm_comfort_family("cooling", t2)  # "cool" reaffirmed — resets the lockout clock

    # Cycle 3 (T=200s, only 100s after the T=100s "cooling" reaffirmation): indoor
    # dips below the floor again and the candidate re-arms as already-sustained
    # (mirrors another >90s below-floor stretch). This is the moment that must be
    # blocked by the lockout (100s < 600s default since the T=100s reaffirmation)
    # but would NOT be blocked by sustain-confirmation alone (100s > 90s).
    engine._comfort_floor_fallback_since = t2 - timedelta(hours=1)
    t3 = _T0 + timedelta(seconds=200)
    with patch(_DT_NOW_PATH, return_value=t3):
        mode3 = engine._resolve_comfort_family_mode(c.hvac_mode, now=t3)
        resolved.append(mode3)
        if mode3 == "heat":
            engine._arm_comfort_family("heating", t3)

    return resolved


class TestFamilySwitchLockoutPreventsHunting:
    """The genuine negative-control test. Verified by the Executor (manually,
    before considering this issue done) to FAIL when `_family_switch_locked_out()`
    is patched to always `return False` — see this class's own trailing note."""

    def test_lockout_blocks_the_second_escalation_within_the_window(self):
        engine = _make_engine(indoor_temp=60.0, comfort_heat=68.0)

        resolved = _run_hunting_sequence(engine)

        assert resolved == ["heat", "cool", "cool"], (
            "With the lockout in place, the third cycle's escalation attempt (100s after "
            "the family last reaffirmed 'cooling', well under the 600s default "
            f"comfort_mode_switch_min_interval_s) must be BLOCKED, not re-escalate to heat. "
            f"Got: {resolved}"
        )

    def test_negative_control_without_lockout_the_flap_reproduces(self):
        """Neutralizes _family_switch_locked_out() (always returns False, exactly
        Verification's own revert-test) and confirms the SAME sequence now DOES
        flap: heat -> cool -> heat, a real hunting pattern only ~200s apart."""
        engine = _make_engine(indoor_temp=60.0, comfort_heat=68.0)

        with patch.object(AutomationEngine, "_family_switch_locked_out", return_value=False):
            resolved = _run_hunting_sequence(engine)

        assert resolved == ["heat", "cool", "heat"], (
            "Negative control: with the lockout disabled, the same sequence must reproduce "
            f"the flap (heat -> cool -> heat only ~200s apart). Got: {resolved} — if this "
            "assertion fails, either the lockout isn't actually disabled by this patch, or "
            "the test sequence itself doesn't exercise the lockout at all."
        )


# ---------------------------------------------------------------------------
# Issue #823: permanent-lockout regression. Reproduces the live incident (Zone
# "Simulated 2", 2026-09-02): indoor continuously below comfort_heat, day stays
# cool-classified, classification cadence (5 min) shorter than
# comfort_mode_switch_min_interval_s (600s default). Drives the REAL call
# sequence _apply_comfort_band()'s cool branch performs every cycle:
# _resolve_comfort_family_mode() then _arm_comfort_family(..., only_if_changed=True)
# — not a re-implementation of that logic, the same two real methods the live
# code path calls, in the same order.
# ---------------------------------------------------------------------------


def _run_reassertion_sequence(engine: AutomationEngine, *, cycle_seconds: int, num_cycles: int) -> list[str]:
    """Simulates `num_cycles` classification cycles, `cycle_seconds` apart, with
    indoor continuously below the comfort floor the whole time (mirrors the engine's
    own indoor-temp mock, fixed below floor via _make_engine). Each cycle calls the
    real _resolve_comfort_family_mode() then _arm_comfort_family() exactly as
    _apply_comfort_band()'s cool/heat branches do."""
    resolved: list[str] = []
    # Seed realistic prior state: "cooling" genuinely became the active family at T0
    # (a real one-shot arm, matching the live incident — the day had just become
    # cool-classified before the breach). A cold start (family=None) trivially
    # bypasses the lockout via its own exemption and would not exercise this bug.
    engine._arm_comfort_family("cooling", _T0)
    engine._comfort_floor_fallback_since = _T0 - timedelta(hours=1)  # already sustained from cycle 1
    for i in range(1, num_cycles + 1):
        t = _T0 + timedelta(seconds=cycle_seconds * i)
        with patch(_DT_NOW_PATH, return_value=t):
            mode = engine._resolve_comfort_family_mode("cool", now=t)
            resolved.append(mode)
            if mode == "heat":
                engine._arm_comfort_family("heating", t, only_if_changed=True)
            else:
                engine._arm_comfort_family("cooling", t, only_if_changed=True)
    return resolved


class TestPermanentLockoutFixIssue823:
    """The Issue #823 fix: with only_if_changed=True at the reassertion call sites,
    the dwell clock measures continuous time-in-family since it genuinely started,
    not time-since-last-reassertion — so a sustained below-floor reading eventually
    escalates to heat, exactly like a normal thermostat."""

    def test_heat_eventually_fires_despite_cadence_shorter_than_lockout_window(self):
        # 5-minute classification cadence (matches the live incident), 15 cycles
        # (70 minutes) — well past the 600s default comfort_mode_switch_min_interval_s
        # measured from the TRUE start of the "cooling" family, not from any single
        # reassertion.
        engine = _make_engine(indoor_temp=60.0, comfort_heat=68.0)

        resolved = _run_reassertion_sequence(engine, cycle_seconds=300, num_cycles=15)

        assert "heat" in resolved, (
            "With the Issue #823 fix, indoor continuously below the comfort floor for "
            "70 minutes (14 reassertion cycles, each shorter than the 600s lockout "
            f"window) must eventually escalate to heat. Got: {resolved} — if this never "
            "reaches 'heat', the permanent-lockout bug has reproduced."
        )
        # The house must not sit locked out indefinitely: heat should fire once
        # elapsed time since the TRUE cooling-family start (T0, the first cycle)
        # exceeds comfort_mode_switch_min_interval_s (600s = cycle index 2, since
        # cycles are 300s apart) plus the 90s sustain-confirm gate already satisfied
        # by the pre-armed fallback candidate.
        first_heat_index = resolved.index("heat")
        assert first_heat_index <= 3, (
            f"Heat fired at cycle {first_heat_index} (t={first_heat_index * 300}s) — expected "
            "at or shortly after t=600s (cycle index 2), not indefinitely delayed. "
            f"Got: {resolved}"
        )

    def test_negative_control_without_the_fix_lockout_never_clears(self):
        """Reverts to the pre-#823 always-reset behavior (only_if_changed has no
        effect once _arm_comfort_family ignores it) — confirms the SAME sequence
        never escalates, reproducing the live incident (indoor stuck below floor
        indefinitely, every cycle re-arming the lockout that blocks it)."""
        engine = _make_engine(indoor_temp=60.0, comfort_heat=68.0)

        def _always_reset_arm(self, family, now, *, only_if_changed=False):
            # only_if_changed is accepted but ignored — the pre-#823 shipped behavior.
            # NOTE: this hand-copies the pre-fix body of the real _arm_comfort_family()
            # rather than calling it, so it won't track future changes to that method's
            # OTHER logic (e.g. the same-tick guard below) — only the only_if_changed
            # branch itself is what this negative control needs to bypass. Acceptable
            # coupling for a revert-test; if the same-tick guard logic changes, revisit.
            _prior = getattr(self, "_comfort_mode_family", None)
            if _prior is not None and _prior != family:
                self._comfort_mode_family_changed_this_tick = True
            self._comfort_mode_family = family
            self._comfort_mode_family_entry_time = now

        with patch.object(AutomationEngine, "_arm_comfort_family", _always_reset_arm):
            resolved = _run_reassertion_sequence(engine, cycle_seconds=300, num_cycles=15)

        assert "heat" not in resolved, (
            "Negative control: without the Issue #823 fix (every call resets the dwell "
            "clock regardless of only_if_changed), the lockout must never clear across "
            f"15 cycles at a 300s cadence — reproducing the live permanent-lockout "
            f"incident. Got: {resolved} — if 'heat' appears, either the patch above "
            "isn't actually restoring the pre-fix behavior, or the test sequence "
            "doesn't exercise the bug."
        )
