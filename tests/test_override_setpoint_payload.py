"""Tests for override_detected event setpoint payload (Fix 5, Issue #290).

Verifies that:
1. handle_manual_override() accepts old_setpoint_f/new_setpoint_f and
   passes them through to the override_detected event payload.
2. ai_skills_activity.py annotation code reads old_setpoint_f/new_setpoint_f
   (not old_temp/new_temp) and emits the [settings: setpoint: X°F→Y°F] annotation.

Source:
  automation.py  handle_manual_override / start_override_confirmation
  ai_skills_activity.py  async_build_activity_context event loop (~line 596)
"""

from __future__ import annotations

import asyncio
import datetime
import sys
from unittest.mock import MagicMock, patch

# ── HA module stubs must be in place before importing climate_advisor modules ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# Patch dt_util.now before import (needed for isoformat() calls in start_override_confirmation)
sys.modules["homeassistant.util.dt"].now = lambda: datetime.datetime(2026, 6, 13, 14, 0, 0)

import custom_components.climate_advisor.ai_skills_activity as _act_mod  # noqa: E402
from custom_components.climate_advisor.ai_skills_activity import (  # noqa: E402
    async_build_activity_context,
)
from custom_components.climate_advisor.automation import AutomationEngine  # noqa: E402
from custom_components.climate_advisor.const import CONF_OVERRIDE_CONFIRM_PERIOD  # noqa: E402

# ---------------------------------------------------------------------------
# Engine stub helpers (mirrors test_setpoint_override.py pattern)
# ---------------------------------------------------------------------------


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    coro.close()


def _make_engine(confirm_seconds: int = 0) -> AutomationEngine:
    """Create an AutomationEngine stub with confirmation disabled by default."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = MagicMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    state = MagicMock()
    state.state = "cool"
    hass.states.get = MagicMock(return_value=state)

    config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
        CONF_OVERRIDE_CONFIRM_PERIOD: confirm_seconds,
    }
    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service="notify.notify",
        config=config,
    )


def _call_handle_manual_override(engine: AutomationEngine, **kwargs) -> list[tuple[str, dict]]:
    """Call handle_manual_override, patching callback/async_call_later so timers don't run.

    Returns the list of (event_type, payload) emitted via _emit_event_callback.
    """
    emitted: list[tuple[str, dict]] = []
    engine._emit_event_callback = lambda et, pl: emitted.append((et, pl))

    with (
        patch("custom_components.climate_advisor.automation.callback", side_effect=lambda fn: fn),
        patch(
            "custom_components.climate_advisor.automation.async_call_later",
            return_value=MagicMock(),
        ),
    ):
        engine.handle_manual_override(**kwargs)

    return emitted


# ---------------------------------------------------------------------------
# TEST 1 — override_detected event payload includes setpoint fields
# ---------------------------------------------------------------------------


class TestOverrideDetectedEventSetpointPayload:
    """handle_manual_override passes old_setpoint_f/new_setpoint_f into the event dict."""

    def test_override_detected_event_includes_setpoint_fields(self):
        """handle_manual_override(old_setpoint_f=72.0, new_setpoint_f=75.0) → event payload
        contains 'old_setpoint_f': 72.0 and 'new_setpoint_f': 75.0."""
        engine = _make_engine(confirm_seconds=600)  # use confirmation window so dedup applies
        emitted = _call_handle_manual_override(
            engine,
            source="setpoint",
            old_mode="cool",
            new_mode="cool",
            old_setpoint_f=72.0,
            new_setpoint_f=75.0,
        )

        override_detected_events = [pl for et, pl in emitted if et == "override_detected"]
        assert override_detected_events, "No override_detected event was emitted"
        payload = override_detected_events[0]
        assert payload.get("old_setpoint_f") == 72.0, (
            f"Expected old_setpoint_f=72.0, got {payload.get('old_setpoint_f')!r}"
        )
        assert payload.get("new_setpoint_f") == 75.0, (
            f"Expected new_setpoint_f=75.0, got {payload.get('new_setpoint_f')!r}"
        )

    def test_override_detected_setpoint_fields_none_when_omitted(self):
        """Mode-only override: old_setpoint_f/new_setpoint_f are None (not absent) in payload."""
        engine = _make_engine(confirm_seconds=600)
        emitted = _call_handle_manual_override(
            engine,
            source="normal",
            old_mode="heat",
            new_mode="cool",
        )

        override_detected_events = [pl for et, pl in emitted if et == "override_detected"]
        assert override_detected_events, "No override_detected event was emitted"
        payload = override_detected_events[0]
        # Keys must be present (so annotation code can call .get() cleanly) but None
        assert "old_setpoint_f" in payload, "old_setpoint_f key missing from payload"
        assert "new_setpoint_f" in payload, "new_setpoint_f key missing from payload"
        assert payload["old_setpoint_f"] is None
        assert payload["new_setpoint_f"] is None


# ---------------------------------------------------------------------------
# Activity context coordinator / hass stubs
# ---------------------------------------------------------------------------


def _make_coord_with_event(event_payload: dict) -> MagicMock:
    """Build a coordinator mock whose _event_log contains one override_detected entry."""
    coord = MagicMock()
    coord.data = {
        "day_type": "mild",
        "trend_direction": "stable",
        "automation_status": "active",
        "occupancy_mode": "home",
        "fan_status": "disabled",
        "contact_status": "all_closed",
        "last_action_time": None,
        "last_action_reason": None,
        "next_automation_action": None,
        "next_automation_time": None,
        "pending_suggestions": [],
    }
    coord.config = {
        "comfort_heat": 68,
        "comfort_cool": 76,
        "setback_heat": 60,
        "setback_cool": 82,
        "wake_time": "06:30",
        "sleep_time": "22:30",
        "briefing_time": "06:00",
        "climate_entity": "climate.thermostat",
        "fan_mode": "disabled",
        "learning_enabled": True,
        "adaptive_preheat_enabled": False,
        "adaptive_setback_enabled": False,
        "weather_bias_enabled": False,
    }
    coord._today_record = MagicMock()
    coord._today_record.hvac_runtime_minutes = 0.0
    coord._hvac_on_since = None
    coord.learning.get_thermal_model.return_value = None
    coord.learning._state.records = []

    # Inject a recent override_detected event
    now = datetime.datetime.now(datetime.UTC)
    entry = {"time": (now - datetime.timedelta(hours=1)).isoformat(), "type": "override_detected"}
    entry.update(event_payload)
    coord._event_log = [entry]

    return coord


def _make_hass() -> MagicMock:
    hass = MagicMock()
    climate_state = MagicMock()
    climate_state.state = "cool"
    climate_state.attributes = {"current_temperature": 73, "temperature": 75}
    hass.states.get.return_value = climate_state
    return hass


# ---------------------------------------------------------------------------
# TEST 2 — annotation code fires with correct field names
# ---------------------------------------------------------------------------


class TestActivityAnnotationSetpointFields:
    """Setpoint data for override_detected is now surfaced via the deterministic table.

    #330: the old inline [settings: setpoint: 72.0°F→75.0°F] annotation in the raw
    EVENT LOG prose block has been replaced by the deterministic timeline table whose
    Settings cell is filled by _render_override_detected.  Tests now assert the table
    path, not the raw annotation string.
    """

    def test_annotation_fires_with_setpoint_data(self):
        """override_detected with old_setpoint_f/new_setpoint_f → Settings cell in table.

        #330: the old assertion looked for '[settings: setpoint: 72.0°F→75.0°F]' in the
        raw context string (EVENT LOG plain-text section).  The new deterministic table
        populates the Settings cell via _render_override_detected, which is verified by
        calling parse_activity_response after async_build_activity_context populates
        _activity_parse_context.  The timeline result must contain 'setpoint:' plus
        the two temperature values.
        """
        from custom_components.climate_advisor.ai_skills_activity import (
            parse_activity_response,
        )

        coord = _make_coord_with_event(
            {
                "old_setpoint_f": 72.0,
                "new_setpoint_f": 75.0,
                "source": "setpoint",
            }
        )
        hass = _make_hass()
        # Patch dt_util.now so _activity_parse_context["now"] is a real tz-aware datetime,
        # not a MagicMock (the ha_stubs dt_util.now() returns MagicMock via __getattr__).
        now_utc = datetime.datetime.now(datetime.UTC)
        with patch.object(_act_mod.dt_util, "now", return_value=now_utc):
            asyncio.run(async_build_activity_context(hass, coord, hours=24))

        # parse_activity_response calls _override_timeline which uses _activity_parse_context
        result = parse_activity_response("## SUMMARY\nTest.\n## DECISIONS\nNone.\n")
        timeline = result["timeline"]

        assert "setpoint:" in timeline, (
            "#330: Settings cell for override_detected must contain 'setpoint:' "
            f"(from _render_override_detected). Timeline:\n{timeline}"
        )
        assert "72" in timeline, (
            f"#330: old_setpoint_f=72.0 must appear in the timeline Settings cell. Got:\n{timeline}"
        )
        assert "75" in timeline, (
            f"#330: new_setpoint_f=75.0 must appear in the timeline Settings cell. Got:\n{timeline}"
        )

    def test_annotation_absent_when_setpoint_fields_none(self):
        """override_detected event with None setpoint fields → no [settings:] annotation."""
        coord = _make_coord_with_event(
            {
                "old_setpoint_f": None,
                "new_setpoint_f": None,
                "old_mode": "heat",
                "new_mode": "cool",
                "source": "normal",
            }
        )
        hass = _make_hass()
        context = asyncio.run(async_build_activity_context(hass, coord, hours=24))

        assert "[settings: setpoint:" not in context, "Expected no setpoint annotation when both fields are None"

    def test_old_temp_key_does_not_trigger_annotation(self):
        """Legacy event with old_temp/new_temp keys (not old_setpoint_f) must NOT
        produce the annotation — confirming the key rename closes the old gap."""
        coord = _make_coord_with_event(
            {
                "old_temp": 72.0,
                "new_temp": 75.0,
                "source": "setpoint",
            }
        )
        hass = _make_hass()
        context = asyncio.run(async_build_activity_context(hass, coord, hours=24))

        # After the fix, only old_setpoint_f/new_setpoint_f trigger the annotation.
        # old_temp/new_temp must not produce a false annotation.
        assert "[settings: setpoint:" not in context, (
            "old_temp/new_temp keys must not trigger the setpoint annotation after the fix"
        )
