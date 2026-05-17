"""Tests for AI activity report context building (Issue #91).

Covers:
- STATE CROSS-VALIDATION section presence in context output
- [WARNING] emitted when hvac_mode=off but hvac_action is an active action
- [FLAG] emitted when indoor temp is outside comfort band
- [OK] emitted when indoor temp is within comfort band
- No flags when state is consistent
- cross-validation absent when temps are non-numeric (graceful skip)
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from unittest.mock import MagicMock

# ── HA module stubs must be in place before importing climate_advisor modules ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# Patch dt_util.now before import
sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 4, 7, 14, 0, 0)

from custom_components.climate_advisor.ai_skills_activity import (  # noqa: E402
    async_build_activity_context,
)
from custom_components.climate_advisor.const import (  # noqa: E402
    ATTR_FAN_STATUS,
    ATTR_HVAC_ACTION,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hass(hvac_mode: str = "heat", current_temp: float | str = 72.0) -> MagicMock:
    """Build a minimal hass mock with a climate entity state."""
    hass = MagicMock()
    climate_state = MagicMock()
    climate_state.state = hvac_mode
    climate_state.attributes = {"current_temperature": current_temp}
    hass.states.get = MagicMock(return_value=climate_state)
    return hass


def _make_coordinator(
    hvac_action: str = "heating",
    comfort_heat: float = 68.0,
    comfort_cool: float = 76.0,
    fan_status: str = "inactive",
) -> MagicMock:
    """Build a minimal coordinator mock."""
    coord = MagicMock()
    coord.data = {ATTR_HVAC_ACTION: hvac_action, ATTR_FAN_STATUS: fan_status}
    coord.config = {
        "climate_entity": "climate.thermostat",
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "setback_heat": 60.0,
        "setback_cool": 82.0,
        "wake_time": "06:30",
        "sleep_time": "22:30",
        "briefing_time": "06:00",
        "learning_enabled": True,
        "adaptive_preheat_enabled": False,
        "adaptive_setback_enabled": False,
        "weather_bias_enabled": False,
        "fan_mode": "disabled",
    }
    coord._today_record = MagicMock()
    coord._today_record.hvac_runtime_minutes = 0.0
    coord._hvac_on_since = None
    # Return None from get_thermal_model so the swing acquisition try-block falls
    # back to THERMAL_SWING_DEFAULT_F instead of a MagicMock that breaks :.1f formatting.
    coord.learning.get_thermal_model.return_value = None
    return coord


def _build_context(
    hvac_mode: str = "heat",
    hvac_action: str = "heating",
    current_temp: float | str = 72.0,
    comfort_heat: float = 68.0,
    comfort_cool: float = 76.0,
    fan_status: str = "inactive",
) -> str:
    """Run async_build_activity_context and return the string."""
    hass = _make_hass(hvac_mode=hvac_mode, current_temp=current_temp)
    coord = _make_coordinator(
        hvac_action=hvac_action,
        comfort_heat=comfort_heat,
        comfort_cool=comfort_cool,
        fan_status=fan_status,
    )
    return asyncio.run(async_build_activity_context(hass, coord))


# ---------------------------------------------------------------------------
# Tests: STATE CROSS-VALIDATION section
# ---------------------------------------------------------------------------


class TestActivityCrossValidationSection:
    """async_build_activity_context includes ## STATE CROSS-VALIDATION (Issue #91)."""

    def test_section_always_present(self):
        """STATE CROSS-VALIDATION section header is always in the context."""
        ctx = _build_context()
        assert "## STATE CROSS-VALIDATION" in ctx

    def test_no_flags_when_consistent(self):
        """No [WARNING]/[FLAG] when hvac_mode matches action and temp is in-band."""
        ctx = _build_context(
            hvac_mode="heat",
            hvac_action="heating",
            current_temp=72.0,
            comfort_heat=68.0,
            comfort_cool=76.0,
        )
        assert "[WARNING]" not in ctx
        assert "[FLAG]" not in ctx
        # [OK] is emitted because temp is in-band; "No contradictions detected" only
        # appears when temps are non-numeric and no other flags were raised.
        assert "[OK]" in ctx

    def test_ok_emitted_when_in_band(self):
        """[OK] line is emitted when temp is within comfort band."""
        ctx = _build_context(
            hvac_mode="heat",
            hvac_action="heating",
            current_temp=72.0,
            comfort_heat=68.0,
            comfort_cool=76.0,
        )
        assert "[OK]" in ctx

    def test_warning_when_hvac_mode_off_action_fan(self):
        """[WARNING] emitted when hvac_mode=off but hvac_action=fan and fan is inactive (stale state)."""
        ctx = _build_context(
            hvac_mode="off",
            hvac_action="fan",
            current_temp=72.0,
            fan_status="inactive",
        )
        assert "[WARNING]" in ctx
        assert "hvac_mode=off" in ctx
        assert "hvac_action='fan'" in ctx

    def test_no_warning_when_hvac_mode_off_action_fan_natural_ventilation(self):
        """No [WARNING] when hvac_mode=off + hvac_action=fan + fan_status=active (natural ventilation)."""
        ctx = _build_context(
            hvac_mode="off",
            hvac_action="fan",
            current_temp=72.0,
            fan_status="active",
        )
        assert "[WARNING]" not in ctx

    def test_no_warning_when_hvac_mode_off_action_fan_manual_override(self):
        """No [WARNING] when hvac_mode=off + hvac_action=fan + fan_status=running (manual override)."""
        ctx = _build_context(
            hvac_mode="off",
            hvac_action="fan",
            current_temp=72.0,
            fan_status="running (manual override)",
        )
        assert "[WARNING]" not in ctx

    def test_warning_when_hvac_mode_off_action_heating(self):
        """[WARNING] emitted when hvac_mode=off but hvac_action=heating."""
        ctx = _build_context(hvac_mode="off", hvac_action="heating")
        assert "[WARNING]" in ctx
        assert "hvac_action='heating'" in ctx

    def test_warning_when_hvac_mode_off_action_cooling(self):
        """[WARNING] emitted when hvac_mode=off but hvac_action=cooling."""
        ctx = _build_context(hvac_mode="off", hvac_action="cooling")
        assert "[WARNING]" in ctx

    def test_no_warning_when_hvac_mode_off_action_idle(self):
        """No [WARNING] when hvac_mode=off and hvac_action=idle (consistent)."""
        ctx = _build_context(hvac_mode="off", hvac_action="idle")
        assert "[WARNING]" not in ctx

    def test_no_warning_when_hvac_mode_off_action_off(self):
        """No [WARNING] when hvac_mode=off and hvac_action=off (consistent)."""
        ctx = _build_context(hvac_mode="off", hvac_action="off")
        assert "[WARNING]" not in ctx

    def test_flag_when_temp_below_comfort_heat(self):
        """[FLAG] emitted when indoor temp < comfort_heat by more than swing."""
        ctx = _build_context(
            hvac_mode="heat",
            hvac_action="heating",
            current_temp=65.0,
            comfort_heat=68.0,
            comfort_cool=76.0,
        )
        assert "[FLAG]" in ctx
        assert "below by" in ctx

    def test_flag_when_temp_above_comfort_cool(self):
        """[FLAG] emitted when indoor temp > comfort_cool by more than swing."""
        ctx = _build_context(
            hvac_mode="cool",
            hvac_action="cooling",
            current_temp=80.0,
            comfort_heat=68.0,
            comfort_cool=76.0,
        )
        assert "[FLAG]" in ctx
        assert "above by" in ctx

    def test_no_flag_when_temp_at_comfort_heat_boundary(self):
        """Temp == comfort_heat is in-band (L <= T is true)."""
        ctx = _build_context(
            hvac_mode="heat",
            hvac_action="heating",
            current_temp=68.0,
            comfort_heat=68.0,
            comfort_cool=76.0,
        )
        assert "[FLAG]" not in ctx
        assert "[OK]" in ctx

    def test_no_flag_when_temp_at_comfort_cool_boundary(self):
        """Temp == comfort_cool is in-band (T <= H is true)."""
        ctx = _build_context(
            hvac_mode="cool",
            hvac_action="cooling",
            current_temp=76.0,
            comfort_heat=68.0,
            comfort_cool=76.0,
        )
        assert "[FLAG]" not in ctx
        assert "[OK]" in ctx

    def test_no_comfort_flag_when_temp_unknown(self):
        """Comfort band check skipped gracefully when current_temp is 'unknown'."""
        ctx = _build_context(
            hvac_mode="heat",
            hvac_action="heating",
            current_temp="unknown",
        )
        # No exception; no FLAG or OK (can't compute)
        assert "[FLAG]" not in ctx
        assert "[OK]" not in ctx

    def test_classification_section_still_present(self):
        """## CLASSIFICATION section is still present after adding cross-validation."""
        ctx = _build_context()
        assert "## CLASSIFICATION" in ctx

    def test_cross_validation_appears_before_classification(self):
        """STATE CROSS-VALIDATION section precedes CLASSIFICATION in the output."""
        ctx = _build_context()
        idx_cv = ctx.index("## STATE CROSS-VALIDATION")
        idx_cl = ctx.index("## CLASSIFICATION")
        assert idx_cv < idx_cl
