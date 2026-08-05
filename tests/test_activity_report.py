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

from custom_components.climate_advisor.ai_skills_context import (  # noqa: E402
    format_engine_status_for_ai as _format_engine_status_for_ai,
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


class TestSetpointEventFieldClarity:
    """Fix F (Issue #277): override_detected setpoint events must render temp
    values in a [settings: ...] annotation, not embedded in the event type
    description.

    Issue #563: the three tests that exercised this through
    async_build_activity_context() (now retired) were removed — the
    integration path they tested no longer exists (the raw duplicate EVENT LOG
    dump they rendered through is superseded by the ACTIVITY TIMELINE
    provider, which uses this same renderer). The remaining test below
    verifies the renderer directly, which is what actually produces the
    [settings:] annotation regardless of which context path renders it.
    """

    def test_system_prompt_instructs_settings_column_for_setpoint_source(self):
        """The Settings column for setpoint events is now filled deterministically, not by LLM.

        #330: the prompt no longer instructs the LLM about the Settings column because
        the entire timeline table — including its Settings cells — is built by
        EVENT_RENDERERS in Python before the response is parsed.  The previous assertion
        tested that the PROMPT contained 'source=setpoint' / Settings routing guidance;
        that guidance is now irrelevant (and absent by design).

        This test now verifies the deterministic renderer path: _render_override_detected
        produces a non-empty Settings cell with setpoint data when old/new setpoint fields
        are present, so the LLM never needs to route them.
        """
        from custom_components.climate_advisor.ai_skills_context import (
            EVENT_RENDERERS,
        )

        renderer = EVENT_RENDERERS["override_detected"]
        payload = {
            "old_setpoint_f": 74,
            "new_setpoint_f": 76,
            "source": "setpoint",
        }
        _ev_text, settings = renderer(payload, "fahrenheit")
        # The Settings cell must contain the setpoint transition — this is what #330 locks
        assert "setpoint:" in settings, (
            f"#330: _render_override_detected must put setpoint values in the Settings cell. Got settings={settings!r}"
        )
        assert "74" in settings and "76" in settings, (
            f"#330: Settings cell must contain both old and new setpoint values. Got: {settings!r}"
        )
