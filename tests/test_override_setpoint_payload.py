"""Tests for override_detected event setpoint payload (Fix 5, Issue #290).

Verifies that:
1. handle_manual_override() accepts old_setpoint_f/new_setpoint_f and
   passes them through to the override_detected event payload.
2. _render_override_detected() (ai_skills_context.py, moved from the retired
   ai_skills_activity.py — Issue #563) reads old_setpoint_f/new_setpoint_f
   (not old_temp/new_temp) and emits the setpoint transition in its Settings
   cell.

Source:
  automation.py  handle_manual_override / start_override_confirmation
  ai_skills_context.py  _render_override_detected
"""

from __future__ import annotations

import datetime
import sys
from unittest.mock import MagicMock, patch

# ── HA module stubs must be in place before importing climate_advisor modules ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# Patch dt_util.now before import (needed for isoformat() calls in start_override_confirmation)
sys.modules["homeassistant.util.dt"].now = lambda: datetime.datetime(2026, 6, 13, 14, 0, 0)

from custom_components.climate_advisor.ai_skills_context import EVENT_RENDERERS  # noqa: E402
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
# TEST 2 — _render_override_detected fires with correct field names (Issue #563:
# ported from the retired async_build_activity_context integration test, which
# checked this through the now-defunct raw event-log annotation string instead
# of the renderer directly)
# ---------------------------------------------------------------------------


class TestOverrideDetectedRendererSetpointFields:
    """Setpoint data for override_detected is surfaced via _render_override_detected's
    Settings cell, keyed on old_setpoint_f/new_setpoint_f — not old_temp/new_temp."""

    def test_settings_cell_contains_setpoint_data(self):
        _ev, settings = EVENT_RENDERERS["override_detected"](
            {"old_setpoint_f": 72.0, "new_setpoint_f": 75.0, "source": "setpoint"},
            "fahrenheit",
        )
        assert "setpoint:" in settings
        assert "72" in settings
        assert "75" in settings

    def test_settings_cell_empty_when_setpoint_fields_none(self):
        _ev, settings = EVENT_RENDERERS["override_detected"](
            {"old_setpoint_f": None, "new_setpoint_f": None, "old_mode": "heat", "new_mode": "cool"},
            "fahrenheit",
        )
        assert "setpoint:" not in settings

    def test_old_temp_key_does_not_trigger_setpoint_annotation(self):
        """Legacy event with old_temp/new_temp keys (not old_setpoint_f) must NOT
        produce a setpoint annotation — confirming the key rename closes the old gap."""
        _ev, settings = EVENT_RENDERERS["override_detected"](
            {"old_temp": 72.0, "new_temp": 75.0, "source": "setpoint"},
            "fahrenheit",
        )
        assert "setpoint:" not in settings
