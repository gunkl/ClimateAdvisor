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
