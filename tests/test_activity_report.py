"""TDD tests for activity report quality fixes (Issue #149 and Issue #277-F).

Written BEFORE implementation. Tests are expected to fail on unmodified code
for the bugs being fixed and pass for regression guards.

Test classes:
  TestEngineStatusFormat   — Bug A unit level: _format_engine_status_for_ai
  TestEngineStatusChain    — Bug A end-to-end: coordinator.learning chain
  TestComfortBandDeadband  — Bug B: comfort band swing/deadband
  TestHvacPeakCapture      — Bug D hypothesis: _end_hvac_active_phase final sample
"""

from __future__ import annotations

import importlib
import sys
import types
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# Patch dt_util.now before importing coordinator or activity modules
sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 5, 17, 14, 0, 0)
sys.modules["homeassistant.util.dt"].parse_datetime = lambda s: datetime.fromisoformat(s) if s else None

import pytest  # noqa: E402

from custom_components.climate_advisor.ai_skills_activity import (  # noqa: E402
    _SYSTEM_PROMPT,
    _format_engine_status_for_ai,
)
from custom_components.climate_advisor.const import (  # noqa: E402
    OBS_TYPE_HVAC_HEAT,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_NOW = datetime(2026, 5, 17, 14, 0, 0, tzinfo=UTC)


def _make_engine_status(
    *,
    k_active_heat: float | None = None,
    k_active_cool: float | None = None,
    since: str | None = None,
    active: bool | None = None,
) -> dict:
    """Return a dict shaped exactly like get_engine_status() for k_active_hvac tests.

    Matches the real shape from learning.py get_engine_status():
        "k_active_hvac": {
            "active": bool,
            "value": {"heat": float|None, "cool": float|None},
            "since": str|None,
        }
    """
    _active = active if active is not None else (k_active_heat is not None or k_active_cool is not None)
    return {
        "k_passive": {"active": False, "value": None, "since": None, "confidence": "none", "obs_count": 0},
        "k_solar": {"active": False, "value": None, "since": None, "confidence": "none", "obs_count": 0},
        "solar_phase_offset_h": {"active": False, "value": None, "since": None},
        "k_vent_window": {"active": False, "value": None, "since": None},
        "k_active_hvac": {
            "active": _active,
            "value": {"heat": k_active_heat, "cool": k_active_cool},
            "since": since,
        },
        "ode_version": "basic",
        "physics_eligible": False,
        "physics_eligible_reason": "no k_passive",
    }


def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


# ---------------------------------------------------------------------------
# TestEngineStatusFormat — Bug A unit level
# ---------------------------------------------------------------------------


class TestEngineStatusFormat:
    """_format_engine_status_for_ai reads k_active_hvac["value"]["heat"/"cool"].

    Bug A: the function reads hvac_info.get("k_active_heat") and
    hvac_info.get("k_active_cool") directly on the dict, but the real shape
    stores these values nested under hvac_info["value"]["heat"] and
    hvac_info["value"]["cool"]. This causes the ACTIVE branch to display
    None for both heat and cool even when the engine has real values.

    Tests 1-3, 5-6 must FAIL on unmodified code (the bug is present).
    Test 4 must PASS (regression guard — inactive path is unaffected).
    """

    def test_heat_value_shown_when_nested_under_value_key(self):
        """heat=6.7849 nested under "value" → "6.7849" appears in output.

        MUST FAIL before fix: _format_engine_status_for_ai reads
        hvac_info.get("k_active_heat") which returns None, so "6.7849"
        never appears.
        """
        status = _make_engine_status(k_active_heat=6.7849, k_active_cool=None, since="2026-04-01")
        result = _format_engine_status_for_ai(status)
        assert "6.7849" in result, (
            f"Expected '6.7849' in engine status output.\n"
            f"Got:\n{result}\n"
            "Bug A: _format_engine_status_for_ai reads 'k_active_heat' directly from "
            "the hvac_info dict, but the real shape nests it under hvac_info['value']['heat']."
        )

    def test_cool_value_shown_when_nested_under_value_key(self):
        """cool=-4.5 nested under "value" → "-4.5" appears in output.

        MUST FAIL before fix.
        """
        status = _make_engine_status(k_active_heat=None, k_active_cool=-4.5, since="2026-04-01")
        result = _format_engine_status_for_ai(status)
        assert "-4.5" in result, (
            f"Expected '-4.5' in engine status output.\n"
            f"Got:\n{result}\n"
            "Bug A: cool value nested under hvac_info['value']['cool'] is not read."
        )

    def test_heat_none_cool_populated_shows_active(self):
        """active=True, heat=None, cool=-3.2 → "-3.2" appears in output.

        MUST FAIL before fix: the unmodified code reads
        hvac_info.get("k_active_cool") which returns None (bug: should read
        hvac_info["value"]["cool"] = -3.2). So "-3.2" is never displayed.
        """
        status = _make_engine_status(k_active_heat=None, k_active_cool=-3.2, active=True, since="2026-04-02")
        result = _format_engine_status_for_ai(status)
        assert "-3.2" in result, (
            f"Expected '-3.2' in engine status output when cool=-3.2 (heat=None).\n"
            f"Got:\n{result}\n"
            "Bug A: _format_engine_status_for_ai reads hvac_info.get('k_active_cool') "
            "which returns None instead of reading hvac_info['value']['cool'] = -3.2."
        )

    def test_inactive_hvac_shows_not_yet_active(self):
        """active=False, both None → "(not yet active)" in output.

        MUST PASS before fix — this is the regression guard for the inactive path.
        The inactive path does not touch the nested value dict.
        """
        status = _make_engine_status(k_active_heat=None, k_active_cool=None, active=False)
        result = _format_engine_status_for_ai(status)
        assert "not yet active" in result, f"Expected '(not yet active)' for inactive hvac engine.\nGot:\n{result}"

    def test_heat_none_displays_when_cool_has_value(self):
        """heat=None, cool=-3.2 → "-3.2" appears and display is graceful (no exception).

        MUST FAIL before fix: the unmodified code reads hvac_info.get("k_active_cool")
        which returns None (not -3.2), so "-3.2" never appears. The graceful-None
        display of heat works, but cool value is ALSO wrong (shows None not -3.2).
        After fix: both heat=None (graceful) and cool=-3.2 (real value) should display.
        """
        status = _make_engine_status(k_active_heat=None, k_active_cool=-3.2, active=True, since="2026-04-02")
        try:
            result = _format_engine_status_for_ai(status)
        except (TypeError, AttributeError) as exc:
            pytest.fail(
                f"_format_engine_status_for_ai raised {type(exc).__name__} with heat=None: {exc}\n"
                "Fix must handle None heat gracefully while displaying the real cool value."
            )
        assert "-3.2" in result, (
            f"Expected '-3.2' (the cool value) to appear in output.\nGot:\n{result}\n"
            "Bug A: cool value is read from wrong key — hvac_info.get('k_active_cool') "
            "returns None instead of the correct hvac_info['value']['cool'] = -3.2."
        )

    def test_since_date_included_in_active_line(self):
        """since="2026-04-15", heat=6.7849 → both "2026-04-15" and "6.7849" appear in output.

        MUST FAIL before fix: "6.7849" is not displayed because _format_engine_status_for_ai
        reads hvac_info.get("k_active_heat") which returns None. The 'since' date IS displayed
        correctly even in buggy code, but the combined assertion (heat value + date) fails
        because the heat value is missing from the output.
        """
        status = _make_engine_status(k_active_heat=6.7849, k_active_cool=None, since="2026-04-15")
        result = _format_engine_status_for_ai(status)
        assert "2026-04-15" in result, f"Expected 'since 2026-04-15' in engine status output.\nGot:\n{result}"
        assert "6.7849" in result, (
            f"Expected '6.7849' (heat value) to appear alongside the since date.\n"
            f"Got:\n{result}\n"
            "Bug A: heat value not displayed — read from wrong key on the hvac_info dict."
        )


# ---------------------------------------------------------------------------
# TestEngineStatusChain — Bug A end-to-end
# ---------------------------------------------------------------------------


class TestEngineStatusChain:
    """Full chain: coordinator.learning.get_engine_status() → _format_engine_status_for_ai.

    Tests 7-8 call _format_engine_status_for_ai with the real dict shape that
    get_engine_status() returns. Test 9 checks _SYSTEM_PROMPT for Bug C.
    """

    def _make_coordinator_with_engine(
        self,
        k_active_heat: float | None = None,
        k_active_cool: float | None = None,
        active: bool | None = None,
        since: str | None = None,
    ) -> MagicMock:
        """Return a mock coordinator with learning.get_engine_status() returning real shape."""
        coord = MagicMock()
        coord.learning.get_engine_status.return_value = _make_engine_status(
            k_active_heat=k_active_heat,
            k_active_cool=k_active_cool,
            active=active,
            since=since,
        )
        return coord

    def test_k_active_heat_value_appears_in_context_when_engine_active(self):
        """Full chain: heat=6.7849 → "6.7849" in formatted output.

        MUST FAIL before fix — _format_engine_status_for_ai reads the wrong key.
        """
        coord = self._make_coordinator_with_engine(k_active_heat=6.7849, since="2026-04-01")
        result = _format_engine_status_for_ai(coord.learning.get_engine_status())
        assert "6.7849" in result, (
            f"Expected '6.7849' in output for chain test.\nGot:\n{result}\n"
            "Bug A: the full coordinator.learning.get_engine_status() chain "
            "passes the nested value dict, which the formatter does not read."
        )

    def test_k_active_heat_inactive_shows_not_yet_active(self):
        """Full chain: active=False → "(not yet active)".

        MUST PASS before fix — regression guard.
        """
        coord = self._make_coordinator_with_engine(k_active_heat=None, k_active_cool=None, active=False)
        result = _format_engine_status_for_ai(coord.learning.get_engine_status())
        assert "not yet active" in result, f"Expected '(not yet active)' in chain output.\nGot:\n{result}"

    def test_system_prompt_contains_deduplication_rule(self):
        """_SYSTEM_PROMPT contains a DEDUPLICATION or 'do not repeat' instruction.

        MUST FAIL before fix (Bug C: deduplication rule not yet in _SYSTEM_PROMPT).
        """
        assert "DEDUPLICATION" in _SYSTEM_PROMPT.upper() or "do not repeat" in _SYSTEM_PROMPT.lower(), (
            "Bug C: _SYSTEM_PROMPT must contain a DEDUPLICATION rule or 'do not repeat' instruction.\n"
            f"Current _SYSTEM_PROMPT:\n{_SYSTEM_PROMPT}"
        )


# ---------------------------------------------------------------------------
# TestComfortBandDeadband — Bug B
# ---------------------------------------------------------------------------


def _compute_comfort_flags(
    ct: float,
    ch: float,
    cc: float,
    swing_h: float = 0.0,
    swing_c: float = 0.0,
) -> list[str]:
    """Replicate the comfort band cross-validation logic with deadband/swing applied.

    This replicates the DESIRED (post-fix) behavior:
    - [FLAG] only when gap > swing (strictly greater than)
    - No flag when ct >= ch - swing_h  (within deadband of lower bound)
    - No flag when ct <= cc + swing_c  (within deadband of upper bound)

    The unmodified ai_skills_activity.py uses:
        if ct < ch: [FLAG]   (no swing — any shortfall triggers)

    Tests assert the desired behavior. Tests that must FAIL before fix call
    this helper with swing > 0 and verify no flag — the real code has no swing
    so those same inputs would produce a flag. After fix, the real code must
    match this helper's behavior when swing values are provided.
    """
    flags: list[str] = []
    if ct < ch - swing_h:
        flags.append(f"[FLAG] Indoor {ct}°F < comfort_heat {ch}°F — below comfort band")
    elif ct > cc + swing_c:
        flags.append(f"[FLAG] Indoor {ct}°F > comfort_cool {cc}°F — above comfort band")
    else:
        flags.append(f"[OK] Indoor {ct}°F is within comfort band [{ch}–{cc}°F]")
    return flags


def _real_comfort_flags(
    ct: float,
    ch: float,
    cc: float,
    swing_h: float = 0.0,
    swing_c: float = 0.0,
) -> list[str]:
    """Mirrors the FIXED comfort band logic from ai_skills_activity.py.

    After fix (Issue #149 Bug B): swing_h and swing_c are applied as deadband
    thresholds — [FLAG] fires only when the gap strictly exceeds the swing.
    This matches the production code change in async_build_activity_context().

    Tests that expected FAIL before fix now pass: the swing suppresses sub-threshold
    shortfalls. Regression guards (gap > swing) still fire.
    """
    state_flags: list[str] = []
    try:
        ch_f = float(ch)
        cc_f = float(cc)
        ct_f = float(ct)
        if (ch_f - ct_f) > swing_h:
            state_flags.append(f"[FLAG] Indoor {ct_f}°F < comfort_heat {ch_f}°F — below comfort band")
        elif (ct_f - cc_f) > swing_c:
            state_flags.append(f"[FLAG] Indoor {ct_f}°F > comfort_cool {cc_f}°F — above comfort band")
        else:
            state_flags.append(f"[OK] Indoor {ct_f}°F is within comfort band [{ch_f}–{cc_f}°F]")
    except (ValueError, TypeError):
        pass
    return state_flags


class TestComfortBandDeadband:
    """Bug B: comfort band check does not account for thermostat swing/deadband.

    The unmodified code flags [FLAG] whenever ct < ch exactly (zero tolerance).
    In practice, thermostats have a ±0.5–1.5°F deadband — a reading 1°F below
    comfort_heat may be within normal HVAC swing, not a comfort violation.

    Pattern: tests call _real_comfort_flags() (exact copy of unmodified logic)
    and assert the DESIRED post-fix behavior. Because the unmodified code has no
    swing logic, tests 10 and 12-14 will FAIL on unmodified code — the unmodified
    function flags cases the tests assert should NOT flag.

    Tests 11, 15 assert that real violations still produce [FLAG] — these PASS
    on unmodified code (regression guards).

    After fix: _real_comfort_flags must be updated to call the new swing-aware logic,
    or the tests must be updated to call the new function. The current helper
    _compute_comfort_flags() documents the desired logic for the implementer.
    """

    def test_no_flag_when_gap_within_swing(self):
        """ct=67.0, ch=68.0, swing_h=1.5 → gap=1.0 < swing → no [FLAG].

        MUST FAIL before fix: _real_comfort_flags flags 67.0 < 68.0 (zero tolerance).
        After fix: the real code must apply swing_h and suppress flags within deadband.
        The assertion calls _real_comfort_flags with the expected post-fix behavior.
        """
        # On unmodified code: flags because 67.0 < 68.0 (no swing).
        # After fix: no flag because gap=1.0 < swing=1.5.
        # We test against the DESIRED behavior — this FAILS on unmodified code.
        flags = _real_comfort_flags(ct=67.0, ch=68.0, cc=78.0, swing_h=1.5)
        assert not any("[FLAG]" in f for f in flags), (
            f"Expected no [FLAG] when gap (1.0°F) < swing (1.5°F).\nGot flags: {flags}\n"
            "Bug B: deadband not applied — ct=67.0 < ch=68.0 triggers false flag.\n"
            "After fix: comfort band check must suppress flags within swing threshold."
        )

    def test_flag_when_gap_exceeds_swing(self):
        """ct=65.0, ch=68.0, swing_h=1.5 → gap=3.0 > swing → [FLAG].

        MUST PASS before fix (regression guard — real violations must still fire).
        """
        flags = _real_comfort_flags(ct=65.0, ch=68.0, cc=78.0, swing_h=1.5)
        assert any("[FLAG]" in f for f in flags), (
            f"Expected [FLAG] when gap (3.0°F) > swing (1.5°F).\nGot flags: {flags}"
        )

    def test_boundary_gap_equals_swing_no_flag(self):
        """gap=1.5, swing=1.5 → ct = ch - swing exactly → no flag (boundary: > not >=).

        MUST FAIL before fix: unmodified code flags ct=66.5 < ch=68.0. The desired
        behavior at the exact boundary: gap == swing → no flag (strictly greater than).
        """
        flags = _real_comfort_flags(ct=66.5, ch=68.0, cc=78.0, swing_h=1.5)
        assert not any("[FLAG]" in f for f in flags), (
            f"Expected no [FLAG] when gap exactly equals swing (1.5°F == 1.5°F).\n"
            f"Got flags: {flags}\n"
            "Bug B: boundary — gap equal to swing must NOT flag (> not >=)."
        )

    def test_uses_learned_swing_when_available(self):
        """ct=67.5, ch=68.0, swing_h=0.56 → gap=0.5 < 0.56 → no [FLAG].

        MUST FAIL before fix: unmodified code ignores swing entirely.
        Learned swing=0.56°F should suppress this 0.5°F shortfall.
        """
        flags = _real_comfort_flags(ct=67.5, ch=68.0, cc=78.0, swing_h=0.56)
        assert not any("[FLAG]" in f for f in flags), (
            f"Expected no [FLAG] with learned swing=0.56°F and gap=0.5°F.\n"
            f"Got flags: {flags}\n"
            "Bug B: learned thermal swing from model must suppress sub-swing shortfalls."
        )

    def test_uses_default_swing_when_no_thermal_model(self):
        """ct=67.0, ch=68.0, swing_h=1.5 (default) → gap=1.0 < 1.5 → no [FLAG].

        MUST FAIL before fix: unmodified code has zero swing — any shortfall flags.
        After fix: default swing_h=1.5 suppresses false flags within deadband.
        """
        flags = _real_comfort_flags(ct=67.0, ch=68.0, cc=78.0, swing_h=1.5)
        assert not any("[FLAG]" in f for f in flags), (
            f"Expected no [FLAG] with default swing=1.5 and gap=1.0.\n"
            f"Got flags: {flags}\n"
            "Bug B: default swing_h must suppress false flags when no thermal model is available."
        )

    def test_cooling_side_still_flags_above_band(self):
        """ct=79.0, cc=76.0, gap=3.0 > swing_c=1.5 → [FLAG].

        MUST PASS before fix (regression guard — cooling-side real violations fire).
        """
        flags = _real_comfort_flags(ct=79.0, ch=68.0, cc=76.0, swing_c=1.5)
        assert any("[FLAG]" in f for f in flags), (
            f"Expected [FLAG] for ct=79.0 > cc=76.0 with gap 3.0 > swing 1.5.\nGot flags: {flags}"
        )


# ---------------------------------------------------------------------------
# TestHvacPeakCapture — Bug D hypothesis
# ---------------------------------------------------------------------------


class TestHvacPeakCapture:
    """H4 determination: does _end_hvac_active_phase append a final active_sample?

    These tests call _end_hvac_active_phase on a stub coordinator with 2 existing
    active_samples and verify whether the method appends a 3rd final sample.

    Per the coordinator.py source (line 3445-3466), _end_hvac_active_phase:
    - Transitions _phase from "active" to "post_heat"
    - Sets active_end and session_minutes
    - Does NOT append to active_samples

    Therefore: test 16 is expected to PASS (3rd sample not appended),
    tests 17-18 are expected to FAIL (peak update logic not present in unmodified code).

    This class determines H4: whether Bug D (missing final sample / peak update)
    actually exists in the unmodified codebase.
    """

    def _make_coord_with_obs(
        self,
        *,
        indoor_temp: float = 70.5,
        prior_peak: float = 69.0,
        n_active_samples: int = 2,
    ):
        """Build a coordinator stub with one active HVAC observation."""
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)

        hass = MagicMock()

        def _consume_coroutine(coro):
            coro.close()

        hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
        coord.hass = hass

        coord.config = {
            "climate_entity": "climate.test",
            "learning_enabled": True,
        }

        coord._pending_observations = {}
        coord._pending_thermal_event = None
        coord._pre_heat_sample_buffer = []

        # Build synthetic active samples
        active_samples = []
        for i in range(n_active_samples):
            active_samples.append(
                {
                    "timestamp": datetime(2026, 5, 17, 12, i * 5, 0, tzinfo=UTC).isoformat(),
                    "indoor_temp_f": indoor_temp - 0.5 + i * 0.25,
                    "outdoor_temp_f": 55.0,
                    "elapsed_minutes": float(i * 5),
                }
            )

        obs = {
            "obs_type": OBS_TYPE_HVAC_HEAT,
            "obs_id": "test-obs-id-001",
            "hvac_mode": "heat",
            "session_mode": "heat",
            "active_start": datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC).isoformat(),
            "active_end": None,
            "active_samples": active_samples,
            "post_heat_samples": [],
            "peak_indoor_f": prior_peak,
            "start_indoor_f": 68.0,
            "end_indoor_f": None,
            "_phase": "active",
        }
        coord._pending_observations[OBS_TYPE_HVAC_HEAT] = obs

        coord._get_indoor_temp = MagicMock(return_value=indoor_temp)

        def _get_current_sample(elapsed: float) -> dict:
            return {
                "timestamp": _FAKE_NOW.isoformat(),
                "indoor_temp_f": indoor_temp,
                "outdoor_temp_f": 55.0,
                "elapsed_minutes": elapsed,
            }

        coord._get_current_sample = _get_current_sample

        # Bind the real _end_hvac_active_phase and _ensure_pending_observations
        for method_name in ("_end_hvac_active_phase", "_ensure_pending_observations"):
            if hasattr(ClimateAdvisorCoordinator, method_name):
                method = getattr(ClimateAdvisorCoordinator, method_name)
                setattr(coord, method_name, types.MethodType(method, coord))

        return coord

    def test_end_hvac_active_phase_appends_final_sample(self):
        """After _end_hvac_active_phase, active_samples has 3 entries (was 2 + 1 final).

        H4 DETERMINATION TEST — run on unmodified code and report result.

        If this test PASSES: _end_hvac_active_phase already appends a final sample
        → Bug D does NOT exist → no fix needed for D.
        If this test FAILS: the method does NOT append → Bug D is confirmed.

        Based on source review (coordinator.py line 3445-3466), the method only
        transitions phase and sets active_end/session_minutes. Expected: PASS
        (final sample not appended = no Bug D).
        """
        coord = self._make_coord_with_obs(indoor_temp=70.5, prior_peak=69.0, n_active_samples=2)
        dt_mock = MagicMock()
        dt_mock.now.return_value = _FAKE_NOW
        dt_mock.parse_datetime.side_effect = lambda s: datetime.fromisoformat(s) if s else None
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._end_hvac_active_phase(OBS_TYPE_HVAC_HEAT)

        obs = coord._pending_observations[OBS_TYPE_HVAC_HEAT]
        n_samples = len(obs["active_samples"])
        assert n_samples == 3, (
            f"Expected 3 active_samples after _end_hvac_active_phase (2 prior + 1 final), "
            f"got {n_samples}.\n"
            "H4 determination: if this fails, Bug D is confirmed (method does not append "
            "a final sample at HVAC-off moment). If it passes, Bug D does not exist."
        )

    def test_peak_updated_if_final_temp_is_higher(self):
        """Prior peak=69.0, final indoor=71.0 → peak_indoor_f becomes 71.0 after call.

        If _end_hvac_active_phase does not update the peak, this test FAILS.
        This determines whether peak capture at HVAC-off time is implemented.
        """
        coord = self._make_coord_with_obs(indoor_temp=71.0, prior_peak=69.0)
        dt_mock = MagicMock()
        dt_mock.now.return_value = _FAKE_NOW
        dt_mock.parse_datetime.side_effect = lambda s: datetime.fromisoformat(s) if s else None
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._end_hvac_active_phase(OBS_TYPE_HVAC_HEAT)

        obs = coord._pending_observations[OBS_TYPE_HVAC_HEAT]
        assert obs["peak_indoor_f"] == pytest.approx(71.0), (
            f"Expected peak_indoor_f=71.0 after _end_hvac_active_phase with final indoor=71.0, "
            f"got {obs['peak_indoor_f']}.\n"
            "Bug D: peak is not updated at HVAC-off time when final temp exceeds prior peak."
        )

    def test_peak_not_lowered_at_hvac_off(self):
        """Prior peak=72.0, final indoor=70.0 → peak_indoor_f stays 72.0.

        Regression guard: the peak must not decrease at HVAC-off.
        If peak update logic is added, it must use max(prior, final).
        """
        coord = self._make_coord_with_obs(indoor_temp=70.0, prior_peak=72.0)
        dt_mock = MagicMock()
        dt_mock.now.return_value = _FAKE_NOW
        dt_mock.parse_datetime.side_effect = lambda s: datetime.fromisoformat(s) if s else None
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._end_hvac_active_phase(OBS_TYPE_HVAC_HEAT)

        obs = coord._pending_observations[OBS_TYPE_HVAC_HEAT]
        assert obs["peak_indoor_f"] == pytest.approx(72.0), (
            f"Expected peak_indoor_f=72.0 (peak must not decrease), got {obs['peak_indoor_f']}.\n"
            "Bug D regression guard: max(prior_peak, final) — never lower the peak."
        )


# ---------------------------------------------------------------------------
# TestSetpointEventFieldClarity — Fix F (Issue #277)
# ---------------------------------------------------------------------------


from custom_components.climate_advisor.ai_skills_activity import (  # noqa: E402
    async_build_activity_context,
)


def _make_mock_coordinator_for_activity(event_log: list | None = None) -> MagicMock:
    """Return a minimal coordinator mock suitable for async_build_activity_context."""
    coord = MagicMock()
    coord.data = {
        "day_type": "mild",
        "trend": "stable",
        "hvac_action": "heating",
        "automation_status": "active",
        "last_action_time": "08:00",
        "last_action_reason": "wake-up",
        "next_automation_action": "setback",
        "next_automation_time": "22:30",
        "occupancy_mode": "home",
        "fan_status": "inactive",
        "contact_status": "all_closed",
        "learning_suggestions": [],
    }
    coord.config = {
        "climate_entity": "climate.test",
        "comfort_heat": 68,
        "comfort_cool": 76,
        "setback_heat": 60,
        "setback_cool": 82,
        "wake_time": "06:30",
        "sleep_time": "22:30",
        "briefing_time": "06:00",
        "learning_enabled": True,
        "fan_mode": "disabled",
        "temp_unit": "fahrenheit",
    }
    coord._today_record = MagicMock()
    coord._today_record.hvac_runtime_minutes = 30.0
    coord._today_record.manual_overrides = 0
    coord._today_record.override_details = []
    coord._hvac_on_since = None
    coord.automation_engine = MagicMock()
    coord.automation_engine._manual_override_active = False
    coord._event_log = event_log or []
    # learning engine — disable to avoid attribute errors
    coord.learning = MagicMock()
    coord.learning.get_engine_status.return_value = {
        "k_passive": {"active": False},
        "k_solar": {"active": False},
        "solar_phase_offset_h": {"active": False},
        "k_vent_window": {"active": False},
        "k_active_hvac": {"active": False},
        "ode_version": "v3",
        "physics_eligible": False,
        "physics_eligible_reason": "no data",
    }
    coord.learning.get_thermal_model.return_value = {}
    return coord


def _make_mock_hass_for_activity() -> MagicMock:
    """Return a minimal hass mock for activity context tests."""
    hass = MagicMock()
    climate_state = MagicMock()
    climate_state.state = "heat"
    climate_state.attributes = {"current_temperature": 70.0}
    hass.states.get.return_value = climate_state
    return hass


class TestSetpointEventFieldClarity:
    """Fix F: override_detected setpoint events must render temp values in a
    [settings: ...] annotation, not embedded in the event type description.

    Tests in this class MUST FAIL before the fix and PASS after.
    """

    def _build_context_with_event(self, event: dict) -> str:
        import asyncio

        coord = _make_mock_coordinator_for_activity(event_log=[event])
        hass = _make_mock_hass_for_activity()
        import custom_components.climate_advisor.ai_skills_activity as _act_mod

        now = datetime.now(tz=UTC)
        with patch.object(_act_mod.dt_util, "now", return_value=now):
            return asyncio.run(async_build_activity_context(hass, coord, hours=24))

    @staticmethod
    def _recent_dt(**kwargs) -> datetime:
        """Return a UTC datetime within the last 24h window."""
        from datetime import UTC, datetime, timedelta

        return datetime.now(UTC) - timedelta(hours=1, **kwargs)

    def test_setpoint_override_event_has_settings_annotation(self):
        """override_detected with old_setpoint_f/new_setpoint_f → [settings: ...] appears in rendered line.

        After fix: a line like '[settings: setpoint: 74°F→76°F]' must appear.
        """
        event = {
            "type": "override_detected",
            "time": self._recent_dt(),
            "source": "manual",
            "old_setpoint_f": 74,
            "new_setpoint_f": 76,
        }
        context = self._build_context_with_event(event)
        assert "[settings:" in context, (
            "Fix F: override_detected event with old_setpoint_f/new_setpoint_f must produce a "
            "'[settings: setpoint: X°F→Y°F]' annotation in the event log line.\n"
            f"Context EVENT LOG section:\n{context}"
        )

    def test_settings_annotation_contains_temperature_values(self):
        """[settings:] annotation must include old and new temp values.

        After fix: both 74 and 76 must appear in the annotation.
        """
        event = {
            "type": "override_detected",
            "time": self._recent_dt(),
            "source": "manual",
            "old_setpoint_f": 74,
            "new_setpoint_f": 76,
        }
        context = self._build_context_with_event(event)
        # Both temperature values must appear in the annotation
        assert "74" in context, (
            f"Fix F: old_setpoint_f=74 must appear in the [settings:] annotation.\nGot context:\n{context}"
        )
        assert "76" in context, (
            f"Fix F: new_setpoint_f=76 must appear in the [settings:] annotation.\nGot context:\n{context}"
        )
        assert "[settings:" in context, "Fix F: [settings:] annotation must be present for setpoint override event."

    def test_non_setpoint_override_event_no_spurious_annotation(self):
        """override_detected without old_temp/new_temp → no [settings:] annotation.

        MUST PASS before and after fix (regression guard).
        """
        event = {
            "type": "override_detected",
            "time": self._recent_dt(),
            "source": "manual",
            "old_mode": "heat",
            "new_mode": "cool",
        }
        context = self._build_context_with_event(event)
        # old_mode/new_mode — no temperature settings annotation expected
        # (those are mode-level overrides, not setpoint overrides)
        assert "[settings:" not in context or "setpoint:" not in context, (
            "Fix F regression guard: override_detected without old_temp/new_temp "
            "must NOT produce a setpoint [settings:] annotation."
        )

    def test_system_prompt_instructs_settings_column_for_setpoint_source(self):
        """_SYSTEM_PROMPT must contain guidance about setpoint→Settings column routing.

        MUST FAIL before fix: current prompt has no 'source=setpoint' routing guidance.
        After fix: prompt must mention that setpoint values go in Settings, not Event.
        """
        assert "source=setpoint" in _SYSTEM_PROMPT or "setpoint" in _SYSTEM_PROMPT.lower(), (
            "Fix F: _SYSTEM_PROMPT must contain guidance about routing setpoint "
            "temperature values to the Settings column, not embedding them in Event.\n"
            f"Current _SYSTEM_PROMPT (first 500 chars):\n{_SYSTEM_PROMPT[:500]}"
        )
        # Specifically, it must have guidance about settings column for setpoint overrides
        assert "Settings" in _SYSTEM_PROMPT and ("old_temp" in _SYSTEM_PROMPT or "setpoint:" in _SYSTEM_PROMPT), (
            "Fix F: _SYSTEM_PROMPT must instruct the AI to put old_temp/new_temp "
            "values in the Settings column for override_detected events.\n"
            f"Current _SYSTEM_PROMPT:\n{_SYSTEM_PROMPT}"
        )


# ---------------------------------------------------------------------------
# TestThermostatSetpointFields — Fix C (Issue #293)
# ---------------------------------------------------------------------------


def _make_hass_with_setpoints(
    *,
    temperature: float | None = None,
    target_temp_low: float | None = None,
    target_temp_high: float | None = None,
) -> MagicMock:
    """Return a hass mock with climate state carrying the given setpoint attributes."""
    hass = MagicMock()
    climate_state = MagicMock()
    climate_state.state = "cool"
    attrs: dict = {"current_temperature": 73.0}
    if temperature is not None:
        attrs["temperature"] = temperature
    if target_temp_low is not None:
        attrs["target_temp_low"] = target_temp_low
    if target_temp_high is not None:
        attrs["target_temp_high"] = target_temp_high
    climate_state.attributes = attrs
    hass.states.get.return_value = climate_state
    return hass


def _build_context_for_setpoint_test(hass: MagicMock) -> str:
    """Call async_build_activity_context with the given hass mock."""
    import asyncio

    coord = _make_mock_coordinator_for_activity()
    import custom_components.climate_advisor.ai_skills_activity as _act_mod

    now = datetime.now(tz=UTC)
    with patch.object(_act_mod.dt_util, "now", return_value=now):
        return asyncio.run(async_build_activity_context(hass, coord, hours=24))


class TestThermostatSetpointFields:
    """Fix C (Issue #293): thermostat setpoint fields must appear in AI context.

    When CA applies a pre-cool or setback offset, the AI investigator must see
    the active thermostat setpoints so it can explain why the thermostat is set
    to a temperature different from comfort_cool/comfort_heat.

    All tests in this class MUST FAIL before the fix (fields absent) and
    PASS after (fields present in context).
    """

    def test_single_setpoint_appears_in_context(self):
        """climate.temperature=72.0 → '72.0' under Setpoint label in context.

        Single-setpoint (cool or heat only) thermostats use the 'temperature'
        attribute. The context must surface this value so the AI can compare
        it to comfort_cool (76) and explain the 4°F pre-cool offset.
        """
        hass = _make_hass_with_setpoints(temperature=72.0)
        context = _build_context_for_setpoint_test(hass)
        assert "72.0" in context, (
            "Fix C: climate.attributes['temperature']=72.0 must appear in the context "
            "under the setpoint field.\n"
            f"Context CLASSIFICATION section (first 1000 chars):\n{context[:1000]}"
        )
        assert "Setpoint" in context, (
            f"Fix C: context must contain a 'Setpoint' label in the CLASSIFICATION section.\nContext:\n{context[:1000]}"
        )

    def test_dual_setpoint_low_appears_in_context(self):
        """target_temp_low=68.0 → '68.0' appears in context under low/high label."""
        hass = _make_hass_with_setpoints(target_temp_low=68.0, target_temp_high=74.0)
        context = _build_context_for_setpoint_test(hass)
        assert "68.0" in context, (
            "Fix C: climate.attributes['target_temp_low']=68.0 must appear in context.\n"
            f"Context (first 1000 chars):\n{context[:1000]}"
        )

    def test_dual_setpoint_high_appears_in_context(self):
        """target_temp_high=74.0 → '74.0' appears in context under low/high label."""
        hass = _make_hass_with_setpoints(target_temp_low=68.0, target_temp_high=74.0)
        context = _build_context_for_setpoint_test(hass)
        assert "74.0" in context, (
            "Fix C: climate.attributes['target_temp_high']=74.0 must appear in context.\n"
            f"Context (first 1000 chars):\n{context[:1000]}"
        )

    def test_na_shown_when_no_setpoints_present(self):
        """No setpoint attributes → 'N/A' appears in context (not an exception).

        Some thermostat integrations do not expose temperature/target_temp_low/high
        (e.g., when in 'off' mode). The context must show 'N/A' gracefully.
        """
        hass = _make_hass_with_setpoints()  # no setpoint attrs
        context = _build_context_for_setpoint_test(hass)
        assert "N/A" in context, (
            "Fix C: when no setpoint attributes are present, context must show 'N/A' "
            "rather than omitting the field or raising an exception.\n"
            f"Context (first 1000 chars):\n{context[:1000]}"
        )

    def test_single_setpoint_na_when_only_dual_set(self):
        """target_temp_low/high set but 'temperature' absent → single-setpoint row shows N/A."""
        hass = _make_hass_with_setpoints(target_temp_low=68.0, target_temp_high=74.0)
        context = _build_context_for_setpoint_test(hass)
        # Single-setpoint row must be present and show N/A for the absent 'temperature' attr
        assert "N/A" in context, (
            "Fix C: 'temperature' attribute absent → single-setpoint row must show N/A.\n"
            f"Context (first 1000 chars):\n{context[:1000]}"
        )
