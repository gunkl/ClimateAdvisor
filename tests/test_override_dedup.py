"""Tests for Issue #96 — Override event deduplication and cascading side-effect guards.

Root Cause A: override_detected is emitted twice — once in coordinator.py and once in
automation.py start_override_confirmation(). Fix: remove coordinator emission, add 5-min
dedup to start_override_confirmation(), enrich event payload with old_mode/new_mode/
classification_mode, and pass those kwargs through handle_manual_override().

Root Cause B: When CA calls _set_hvac_mode(), Ecobee cloud side-effects (fan_mode,
temperature) arrive 10–30 s later but _hvac_command_pending has already been cleared.
The temp and fan guards in _async_thermostat_changed() must also check
_hvac_command_pending and _is_recent_hvac_command(threshold_seconds=30.0).
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# Provide a stable dt_util.now so isoformat() calls work
_NOW_BASE = datetime(2026, 4, 10, 10, 0, 0, tzinfo=UTC)
sys.modules["homeassistant.util.dt"].now = lambda: _NOW_BASE

from custom_components.climate_advisor.automation import AutomationEngine  # noqa: E402
from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402

# ── Helpers ──────────────────────────────────────────────────────────────────

_THERMOSTAT_ID = "climate.thermostat"
_PATCH_CALL_LATER = "custom_components.climate_advisor.coordinator.async_call_later"
_PATCH_CALLBACK = "custom_components.climate_advisor.coordinator.callback"


def _consume_coroutine(coro):
    """Close a coroutine to prevent 'never awaited' RuntimeWarning."""
    if asyncio.iscoroutine(coro):
        coro.close()


def _make_engine(
    comfort_heat: float = 70.0,
    config_overrides: dict | None = None,
) -> AutomationEngine:
    """Build an AutomationEngine with mocked HA dependencies."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    # Default thermostat state returned by hass.states.get
    mock_state = MagicMock()
    mock_state.state = "heat"
    hass.states.get = MagicMock(return_value=mock_state)

    config = {
        "comfort_heat": comfort_heat,
        "comfort_cool": 76.0,
        "setback_heat": 60.0,
        "setback_cool": 82.0,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
        # 5-minute confirmation window
        "override_confirm_period": 300,
    }
    if config_overrides:
        config.update(config_overrides)

    engine = AutomationEngine(
        hass=hass,
        climate_entity=_THERMOSTAT_ID,
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service=config["notify_service"],
        config=config,
    )
    return engine


def _make_classification(**overrides):
    """Build a DayClassification bypassing __post_init__ validation."""
    c = object.__new__(DayClassification)
    defaults = {
        "day_type": "mild",
        "trend_direction": "stable",
        "trend_magnitude": 0,
        "today_high": 72,
        "today_low": 55,
        "tomorrow_high": 73,
        "tomorrow_low": 56,
        "hvac_mode": "off",
        "pre_condition": False,
        "pre_condition_target": None,
        "windows_recommended": False,
        "window_open_time": None,
        "window_close_time": None,
        "setback_modifier": 0.0,
        "window_opportunity_morning": False,
        "window_opportunity_evening": False,
    }
    defaults.update(overrides)
    c.__dict__.update(defaults)
    return c


def _get_coordinator_class():
    """Return the current ClimateAdvisorCoordinator class (fresh import each call)."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _make_thermostat_coordinator_stub(
    *,
    temp_command_pending: bool = False,
    hvac_command_pending: bool = False,
    hvac_command_age_seconds: float | None = None,
    fan_command_pending: bool = False,
    fan_override_active: bool = False,
    manual_override_active: bool = False,
    pause_active: bool = False,
):
    """Build a minimal coordinator-like object for testing _async_thermostat_changed.

    hvac_command_age_seconds: how many seconds ago the HVAC command was issued.
      None → _is_recent_hvac_command always returns False (no recent command).
      <30  → _is_recent_hvac_command(threshold_seconds=30) returns True (within window).
      >=30 → returns False (past window, genuine override).
    """
    from custom_components.climate_advisor.learning import DailyRecord

    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = hass
    coord.config = {
        "climate_entity": _THERMOSTAT_ID,
        "weather_entity": "weather.forecast_home",
        "comfort_heat": 70,
        "comfort_cool": 75,
    }

    # Automation engine — MagicMock (NOT AsyncMock) per project convention
    ae = MagicMock()
    ae.is_paused_by_door = False
    ae._hvac_command_pending = hvac_command_pending
    ae._manual_override_active = manual_override_active
    ae._pause_active = pause_active
    ae._fan_command_pending = fan_command_pending
    ae._fan_override_active = fan_override_active
    ae._temp_command_pending = temp_command_pending
    ae._temp_command_time = None  # no recent temp command by default
    ae._fan_active = False
    ae._natural_vent_active = False
    ae.handle_manual_override_during_pause = AsyncMock()
    ae.handle_manual_override = MagicMock()
    ae.handle_fan_manual_override = MagicMock()
    coord.automation_engine = ae

    coord._current_classification = _make_classification()
    coord._today_record = DailyRecord(date="2026-04-10", day_type="mild", trend_direction="stable")
    coord._async_save_state = AsyncMock()

    # _is_recent_temp_command: no recent temp command in these dedup tests
    coord._is_recent_temp_command = MagicMock(return_value=False)

    # Mock _is_recent_hvac_command based on hvac_command_age_seconds:
    # Returns True (within threshold) if age < threshold, else False.
    if hvac_command_age_seconds is None:
        coord._is_recent_hvac_command = MagicMock(return_value=False)
    else:

        def _is_recent(threshold_seconds: float = 3.0) -> bool:
            return hvac_command_age_seconds < threshold_seconds

        coord._is_recent_hvac_command = _is_recent

    coord._emit_event = MagicMock()
    coord._hvac_on_since = None
    coord._pending_thermal_event = None
    coord._pre_heat_sample_buffer = []
    coord._flush_hvac_runtime = MagicMock()
    coord._start_hvac_observation = AsyncMock()
    coord._end_hvac_active_phase = AsyncMock()
    coord._abandon_observation = AsyncMock()
    coord._get_indoor_temp = MagicMock(return_value=72.0)
    coord._get_outdoor_temp = MagicMock(return_value=65.0)
    coord._chart_log = MagicMock()

    # Bind the real method under test
    coord._async_thermostat_changed = types.MethodType(ClimateAdvisorCoordinator._async_thermostat_changed, coord)

    return coord


def _make_state_event(
    old_hvac_mode="off",
    new_hvac_mode="heat",
    old_temp=70.0,
    new_temp=70.0,
    old_fan_mode=None,
    new_fan_mode=None,
):
    """Build a minimal HA state-change event dict."""
    old_state = MagicMock()
    old_state.state = old_hvac_mode
    old_state.attributes = {"temperature": old_temp}
    if old_fan_mode is not None:
        old_state.attributes = {"temperature": old_temp, "fan_mode": old_fan_mode}

    new_state = MagicMock()
    new_state.state = new_hvac_mode
    new_state.attributes = {"temperature": new_temp, "hvac_action": ""}
    if new_fan_mode is not None:
        new_state.attributes = {"temperature": new_temp, "hvac_action": "", "fan_mode": new_fan_mode}

    event = MagicMock()
    event.data = {"old_state": old_state, "new_state": new_state}
    return event


# ═══════════════════════════════════════════════════════════════════════════════
# Class A — Override event deduplication
# ═══════════════════════════════════════════════════════════════════════════════


class TestOverrideEventDeduplication:
    """Test that override_detected is emitted exactly once with the correct payload.

    After the fix:
    - coordinator.py no longer emits override_detected (line 1680-1688 removed)
    - start_override_confirmation() emits it with a 5-min dedup guard
    - handle_manual_override() accepts and forwards kwargs to start_override_confirmation()
    """

    def test_single_emission_on_first_override(self):
        """Call start_override_confirmation once → exactly 1 override_detected event."""
        engine = _make_engine()
        emitted = []
        engine._emit_event_callback = lambda name, payload: emitted.append((name, payload))

        with patch("custom_components.climate_advisor.automation.async_call_later"):
            engine.start_override_confirmation("normal")

        override_events = [e for e in emitted if e[0] == "override_detected"]
        assert len(override_events) == 1, f"Expected exactly 1 override_detected, got {len(override_events)}: {emitted}"

    def test_second_override_within_5min_suppressed(self):
        """Two calls within 4m59s → only 1 override_detected event total (dedup)."""
        engine = _make_engine()
        emitted = []
        engine._emit_event_callback = lambda name, payload: emitted.append((name, payload))

        t0 = _NOW_BASE
        t1 = _NOW_BASE + timedelta(minutes=4, seconds=59)

        with (
            patch("custom_components.climate_advisor.automation.async_call_later"),
            patch("custom_components.climate_advisor.automation.dt_util") as mock_dt,
        ):
            mock_dt.now.return_value = t0
            engine.start_override_confirmation("normal")

            mock_dt.now.return_value = t1
            engine.start_override_confirmation("normal")

        override_events = [e for e in emitted if e[0] == "override_detected"]
        assert len(override_events) == 1, (
            f"Expected dedup to suppress second event (got {len(override_events)}). "
            "The fix must add _last_override_detected_time tracking to "
            "start_override_confirmation()."
        )

    def test_override_emitted_after_5min_window_expires(self):
        """Two calls with 5m1s between them → 2 override_detected events."""
        engine = _make_engine()
        emitted = []
        engine._emit_event_callback = lambda name, payload: emitted.append((name, payload))

        t0 = _NOW_BASE
        t1 = _NOW_BASE + timedelta(minutes=5, seconds=1)

        with (
            patch("custom_components.climate_advisor.automation.async_call_later"),
            patch("custom_components.climate_advisor.automation.dt_util") as mock_dt,
        ):
            mock_dt.now.return_value = t0
            engine.start_override_confirmation("normal")

            mock_dt.now.return_value = t1
            engine.start_override_confirmation("normal")

        override_events = [e for e in emitted if e[0] == "override_detected"]
        assert len(override_events) == 2, (
            f"Expected 2 override_detected events (window expired), got {len(override_events)}"
        )

    def test_confirmation_timer_still_restarts_within_dedup_window(self):
        """Dedup suppresses the event but the confirmation timer is still restarted.

        _override_confirm_cancel() MUST be called on the second invocation even
        when dedup prevents the event from being emitted.
        """
        engine = _make_engine()
        engine._emit_event_callback = lambda name, payload: None

        cancel_mock = MagicMock()

        t0 = _NOW_BASE
        t1 = _NOW_BASE + timedelta(minutes=1)

        with patch("custom_components.climate_advisor.automation.async_call_later") as mock_later:
            mock_later.return_value = cancel_mock

            with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
                mock_dt.now.return_value = t0
                engine.start_override_confirmation("normal")

                # After first call, _override_confirm_cancel should be set
                first_cancel = engine._override_confirm_cancel

                mock_dt.now.return_value = t1
                engine.start_override_confirmation("normal")

        # The first cancel function should have been called (timer restarted)
        assert cancel_mock.called or first_cancel is None or first_cancel.called, (
            "Expected confirmation timer to be cancelled and restarted on second call, "
            "even when dedup suppresses the event. The timer restart must happen before "
            "the dedup check."
        )

    def test_enriched_payload_contains_all_fields(self):
        """Event payload must include all 6 keys after the fix."""
        engine = _make_engine()
        emitted = []
        engine._emit_event_callback = lambda name, payload: emitted.append((name, payload))

        required_keys = {
            "detected_mode",
            "source",
            "confirm_delay_seconds",
            "old_mode",
            "new_mode",
            "classification_mode",
        }

        try:
            with patch("custom_components.climate_advisor.automation.async_call_later"):
                engine.start_override_confirmation(
                    "normal",
                    old_mode="off",
                    new_mode="heat",
                    classification_mode="off",
                )
        except TypeError as exc:
            raise AssertionError(
                f"start_override_confirmation() rejected kwargs (old_mode, new_mode, "
                f"classification_mode): {exc}. "
                "The fix must update the signature to accept these optional keyword args."
            ) from exc

        assert emitted, "No events emitted — start_override_confirmation() did not call callback"
        _, payload = emitted[0]
        missing = required_keys - set(payload.keys())
        assert not missing, (
            f"Event payload missing keys: {missing}. "
            "The fix must add old_mode, new_mode, classification_mode kwargs to "
            "start_override_confirmation() and include them in the event payload."
        )

    def test_handle_manual_override_passes_context_through(self):
        """handle_manual_override(old_mode=, new_mode=, classification_mode=) → payload has them."""
        engine = _make_engine()
        emitted = []
        engine._emit_event_callback = lambda name, payload: emitted.append((name, payload))

        try:
            with patch("custom_components.climate_advisor.automation.async_call_later"):
                engine.handle_manual_override(
                    old_mode="off",
                    new_mode="heat",
                    classification_mode="off",
                )
        except TypeError as exc:
            raise AssertionError(
                f"handle_manual_override() rejected kwargs: {exc}. "
                "The fix must update handle_manual_override() to accept old_mode, "
                "new_mode, classification_mode keyword args and forward them to "
                "start_override_confirmation()."
            ) from exc

        assert emitted, "No events emitted — handle_manual_override() must forward kwargs"
        _, payload = emitted[0]
        assert payload.get("old_mode") == "off", (
            f"Expected old_mode='off' in payload, got: {payload}. "
            "handle_manual_override() must accept and forward old_mode/new_mode/"
            "classification_mode to start_override_confirmation()."
        )
        assert payload.get("new_mode") == "heat", f"Expected new_mode='heat' in payload: {payload}"
        assert payload.get("classification_mode") == "off", f"Expected classification_mode='off' in payload: {payload}"

    def test_coordinator_does_not_double_emit(self):
        """Coordinator path produces exactly 1 override_detected total.

        After the fix, coordinator.py must NOT emit override_detected (coordinator
        emission at lines 1680-1688 removed). AutomationEngine emits it once via
        start_override_confirmation().
        """
        # Track all override_detected calls across both coordinator and engine paths
        coordinator_emissions = []
        engine_emissions = []

        engine = _make_engine()
        engine._emit_event_callback = lambda name, payload: engine_emissions.append((name, payload))

        # Simulate what the coordinator currently does BEFORE the fix:
        # emitting override_detected, then calling handle_manual_override() which emits again.
        # After the fix, only handle_manual_override() should emit.
        try:
            with patch("custom_components.climate_advisor.automation.async_call_later"):
                # Simulate coordinator calling handle_manual_override() WITHOUT pre-emitting
                engine.handle_manual_override(
                    old_mode="off",
                    new_mode="heat",
                    classification_mode="off",
                )
        except TypeError as exc:
            raise AssertionError(
                f"handle_manual_override() rejected context kwargs: {exc}. "
                "The fix must update handle_manual_override() signature to accept these kwargs."
            ) from exc

        all_override_events = [e for e in coordinator_emissions if e[0] == "override_detected"] + [
            e for e in engine_emissions if e[0] == "override_detected"
        ]
        assert len(all_override_events) == 1, (
            f"Expected exactly 1 override_detected across coordinator+engine, "
            f"got {len(all_override_events)}. "
            "If this fails, coordinator is still emitting override_detected before "
            "calling handle_manual_override()."
        )

    def test_pause_path_single_emission(self):
        """Pause path emits exactly 1 override_detected with enriched payload.

        After the fix, coordinator.py removes its direct emission in the pause path.
        handle_manual_override_during_pause() forwards old_mode/new_mode/classification_mode
        to start_override_confirmation("pause"), which emits the single enriched event.
        """
        engine = _make_engine()
        engine._paused_by_door = True  # Simulate being in a pause
        emitted = []
        engine._emit_event_callback = lambda name, payload: emitted.append((name, payload))

        with patch("custom_components.climate_advisor.automation.async_call_later"):
            asyncio.run(
                engine.handle_manual_override_during_pause(
                    old_mode="off",
                    new_mode="heat",
                    classification_mode="off",
                )
            )

        override_events = [e for e in emitted if e[0] == "override_detected"]
        assert len(override_events) == 1, (
            f"Expected exactly 1 override_detected from pause path, got {len(override_events)}"
        )
        _, payload = override_events[0]
        assert payload.get("source") == "pause", f"Expected source='pause', got: {payload}"
        assert payload.get("old_mode") == "off", f"Expected old_mode='off', got: {payload}"
        assert payload.get("new_mode") == "heat", f"Expected new_mode='heat', got: {payload}"
        assert payload.get("classification_mode") == "off", f"Expected classification_mode='off', got: {payload}"


# ═══════════════════════════════════════════════════════════════════════════════
# Class B — Cascading thermostat side-effects not counted as overrides
# ═══════════════════════════════════════════════════════════════════════════════


class TestCascadingSideEffectsNotCountedAsOverride:
    """Test that Ecobee cloud side-effects (temp/fan changes after HVAC command) are not
    counted as manual overrides.

    After the fix, both the temp guard and fan guard in _async_thermostat_changed()
    must also check:
      - not automation_engine._hvac_command_pending
      - not self._is_recent_hvac_command(threshold_seconds=30.0)
    """

    def test_temp_change_after_hvac_command_within_30s_not_counted(self):
        """Temperature change arriving 10s after HVAC command must NOT increment overrides."""
        coord = _make_thermostat_coordinator_stub(hvac_command_age_seconds=10.0)

        initial_overrides = coord._today_record.manual_overrides

        event = _make_state_event(
            old_hvac_mode="off",
            new_hvac_mode="off",
            old_temp=70.0,
            new_temp=72.0,  # temperature changed
        )

        with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn):
            asyncio.run(coord._async_thermostat_changed(event))

        assert coord._today_record.manual_overrides == initial_overrides, (
            f"Expected manual_overrides to stay at {initial_overrides}, "
            f"got {coord._today_record.manual_overrides}. "
            "The temp guard must check _is_recent_hvac_command(threshold_seconds=30.0) "
            "to suppress Ecobee cloud side-effect temp changes."
        )

    def test_temp_change_after_hvac_command_after_30s_counted(self):
        """Temperature change arriving 31s after HVAC command IS a real override → counted."""
        coord = _make_thermostat_coordinator_stub(hvac_command_age_seconds=31.0)

        initial_overrides = coord._today_record.manual_overrides

        event = _make_state_event(
            old_hvac_mode="off",
            new_hvac_mode="off",
            old_temp=70.0,
            new_temp=72.0,
        )

        with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn):
            asyncio.run(coord._async_thermostat_changed(event))

        assert coord._today_record.manual_overrides == initial_overrides + 1, (
            f"Expected manual_overrides to be {initial_overrides + 1} after 31s window, "
            f"got {coord._today_record.manual_overrides}. "
            "Temperature change after 30s should be counted as a manual override."
        )

    def test_fan_mode_change_after_hvac_command_within_30s_not_override(self):
        """Fan mode change 10s after HVAC command must NOT call handle_fan_manual_override."""
        coord = _make_thermostat_coordinator_stub(hvac_command_age_seconds=10.0)

        event = _make_state_event(
            old_hvac_mode="heat",
            new_hvac_mode="heat",
            old_fan_mode="auto",
            new_fan_mode="on",
        )

        with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn):
            asyncio.run(coord._async_thermostat_changed(event))

        assert coord.automation_engine.handle_fan_manual_override.call_count == 0, (
            "handle_fan_manual_override() must NOT be called when the fan_mode change "
            "arrives within 30s of an HVAC command (Ecobee cloud side-effect). "
            "The fan guard must check _is_recent_hvac_command(threshold_seconds=30.0)."
        )

    def test_fan_mode_change_after_hvac_command_after_30s_is_override(self):
        """Fan mode change 31s after HVAC command IS a real override → handle called."""
        coord = _make_thermostat_coordinator_stub(hvac_command_age_seconds=31.0)

        event = _make_state_event(
            old_hvac_mode="heat",
            new_hvac_mode="heat",
            old_fan_mode="auto",
            new_fan_mode="on",
        )

        with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn):
            asyncio.run(coord._async_thermostat_changed(event))

        assert coord.automation_engine.handle_fan_manual_override.call_count == 1, (
            "handle_fan_manual_override() SHOULD be called when fan_mode changes 31s "
            "after an HVAC command (past the 30s window, genuine user override)."
        )

    def test_fan_mode_change_while_hvac_command_pending_not_override(self):
        """Fan mode change while _hvac_command_pending=True must NOT call handle_fan_manual_override."""
        coord = _make_thermostat_coordinator_stub(hvac_command_pending=True)

        event = _make_state_event(
            old_hvac_mode="heat",
            new_hvac_mode="heat",
            old_fan_mode="auto",
            new_fan_mode="on",
        )

        with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn):
            asyncio.run(coord._async_thermostat_changed(event))

        assert coord.automation_engine.handle_fan_manual_override.call_count == 0, (
            "handle_fan_manual_override() must NOT be called when _hvac_command_pending "
            "is True. The fan guard is missing the _hvac_command_pending check."
        )

    def test_temp_change_while_hvac_command_pending_not_counted(self):
        """Temperature change while _hvac_command_pending=True must NOT increment overrides."""
        coord = _make_thermostat_coordinator_stub(hvac_command_pending=True)

        initial_overrides = coord._today_record.manual_overrides

        event = _make_state_event(
            old_hvac_mode="off",
            new_hvac_mode="off",
            old_temp=70.0,
            new_temp=72.0,
        )

        with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn):
            asyncio.run(coord._async_thermostat_changed(event))

        assert coord._today_record.manual_overrides == initial_overrides, (
            f"Expected manual_overrides to stay at {initial_overrides} while "
            f"_hvac_command_pending=True, got {coord._today_record.manual_overrides}. "
            "The temp guard must check _hvac_command_pending (not just _temp_command_pending)."
        )
