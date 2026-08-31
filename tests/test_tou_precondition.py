"""Tests for the Time-of-Use (TOU) scheduler's pre-conditioning/coast behavior (Issue #786).

Covers the plan's mandatory prerequisite check before any new "coast phase" guard code is
written: does ``_apply_comfort_band()`` re-issue a mid-band setpoint every cycle regardless of
where indoor temp sits, or does it only correct at the band's active edge? The answer determines
whether TOU pre-conditioning needs any new suppression logic once a scheduled high-cost window
begins, or whether the existing single-setpoint threshold command already provides "coast until
the safety edge is breached" for free.

Finding (confirmed against both direct code reading and the existing
``test_warm_day_comfort_gap.py`` docstring/tests, which already document the same mechanism for
the unrelated warm-day case): ``_apply_comfort_band()`` issues ONE ``set_temperature`` call with
``hvac_mode`` — a threshold command a real thermostat only acts on when indoor crosses that
threshold, not a proactive drive-to-target. Re-arming the same edge value every 30-minute cycle
is a no-op while indoor sits on the correct side of it. Therefore: TOU banking needs new code
only for the PRE-CONDITIONING phase (push toward the *opposite* edge ahead of the window); once
pre-conditioning stops being called, the normal cycle's `_apply_comfort_band()` call already
re-arms the correct "coast until this edge" threshold with zero new guard code.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

_STUBS = Path(__file__).parent / "stubs"
if _STUBS.exists() and str(_STUBS) not in sys.path:
    sys.path.insert(0, str(_STUBS))

from custom_components.climate_advisor.automation import (  # noqa: E402
    AutomationEngine,
)
from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402
from custom_components.climate_advisor.const import (  # noqa: E402
    CLIMATE_FEATURE_TARGET_TEMP_RANGE,
    CONF_FAN_MODE,
    FAN_MODE_WHOLE_HOUSE,
)


def _consume_coroutine(coro):
    coro.close()


def _make_engine(
    *,
    indoor_temp: float,
    comfort_heat: float = 68.0,
    comfort_cool: float = 76.0,
    current_mode: str = "cool",
) -> AutomationEngine:
    """Build a dual-capable AutomationEngine, mirroring test_warm_day_comfort_gap.py's helper."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    attrs = {
        "hvac_modes": ["off", "heat", "cool", "heat_cool"],
        "supported_features": CLIMATE_FEATURE_TARGET_TEMP_RANGE,
        "current_temperature": indoor_temp,
    }
    climate_state = MagicMock()
    climate_state.state = current_mode
    climate_state.attributes = attrs
    hass.states.get.return_value = climate_state

    config = {
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "setback_heat": 60.0,
        "setback_cool": 82.0,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
    }

    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service=config["notify_service"],
        config=config,
    )


def _make_cool_day_classification() -> DayClassification:
    """A cooling day: select_comfort_band()'s active edge is the ceiling (comfort_cool)."""
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


class TestApplyComfortBandEdgeOnlyIntervention:
    """Prerequisite check (plan Execution Structure): confirms no new coast-phase code is needed.

    A TOU pre-conditioning phase drives indoor temp to the *opposite* edge of the comfort band
    (e.g. comfort_heat, banking coolness) ahead of a high-cost cooling window. If
    ``_apply_comfort_band()`` computed a setpoint proportional to current indoor position, or
    otherwise behaved differently when indoor sits far from the normally-active edge, a new
    suppression/coast guard would be required. These tests prove it does not: the command is
    identical regardless of starting position.
    """

    def test_same_edge_and_mode_commanded_whether_banked_or_not(self):
        """Indoor banked at the far (floor) edge vs. already at the ceiling: identical command.

        This is the crux of the prerequisite: if indoor sitting at comfort_heat (68°F, freshly
        banked by TOU pre-conditioning) produced a *different* set_temperature call than indoor
        already at comfort_cool (76°F), the "coast" behavior would need new code to reconcile
        the difference. It does not — both produce the exact same single threshold command.
        """
        banked_engine = _make_engine(indoor_temp=68.0, comfort_heat=68.0, comfort_cool=76.0)
        unbanked_engine = _make_engine(indoor_temp=76.0, comfort_heat=68.0, comfort_cool=76.0)
        classification = _make_cool_day_classification()

        asyncio.run(banked_engine.apply_classification(classification))
        asyncio.run(unbanked_engine.apply_classification(classification))

        def _temp_call(engine):
            calls = engine.hass.services.async_call.call_args_list
            temp_calls = [c for c in calls if c.args[1] == "set_temperature"]
            assert len(temp_calls) == 1, "expected exactly one set_temperature call per cycle"
            return temp_calls[0].args[2]

        banked_payload = _temp_call(banked_engine)
        unbanked_payload = _temp_call(unbanked_engine)

        assert banked_payload == unbanked_payload
        assert banked_payload["hvac_mode"] == "cool"
        assert banked_payload["temperature"] == 76.0

    def test_command_is_a_threshold_not_a_drive_to_target(self):
        """The single set_temperature call always targets the band's active edge value itself —
        never a value interpolated toward current indoor temp — confirming it is a static
        threshold for the real thermostat to act on, not a proactive push computed by CA.
        """
        engine = _make_engine(indoor_temp=69.0, comfort_heat=68.0, comfort_cool=76.0)
        classification = _make_cool_day_classification()

        asyncio.run(engine.apply_classification(classification))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c.args[1] == "set_temperature"]
        assert len(temp_calls) == 1
        payload = temp_calls[0].args[2]
        # Must equal comfort_cool exactly (76.0), not some value between 69.0 and 76.0.
        assert payload["temperature"] == 76.0
        assert payload["hvac_mode"] == "cool"


class TestApplyTouPrecondition:
    """AutomationEngine.apply_tou_precondition() (Issue #786)."""

    def test_commands_target_override_not_plain_comfort_value(self):
        """The banked target (e.g. sleep-aware) must be commanded, not self.config's plain
        comfort_heat/comfort_cool — this is the whole point of target_override."""
        engine = _make_engine(indoor_temp=76.0, comfort_heat=68.0, comfort_cool=76.0)
        classification = _make_cool_day_classification()

        asyncio.run(engine.apply_tou_precondition(classification, target=64.0, schedule_id="s1"))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c.args[1] == "set_temperature"]
        assert len(temp_calls) == 1
        payload = temp_calls[0].args[2]
        assert payload["temperature"] == 64.0
        assert payload["hvac_mode"] == "cool"

    def test_heat_mode_commands_heat_target(self):
        engine = _make_engine(indoor_temp=68.0, comfort_heat=68.0, comfort_cool=76.0, current_mode="heat")
        classification = _make_cool_day_classification()
        classification.hvac_mode = "heat"

        asyncio.run(engine.apply_tou_precondition(classification, target=78.0, schedule_id="s2"))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c.args[1] == "set_temperature"]
        assert len(temp_calls) == 1
        payload = temp_calls[0].args[2]
        assert payload["temperature"] == 78.0
        assert payload["hvac_mode"] == "heat"

    def test_skips_when_monitored_door_window_open(self):
        """Mirrors _apply_comfort_band()'s own door/window guard (Issue #629) — no HVAC
        command is issued while a monitored sensor is open. Uses the same
        `_sensor_check_callback` direct-assignment pattern as test_door_window.py, since
        `_any_monitored_sensor_open()` reads that callback rather than hass.states directly."""
        engine = _make_engine(indoor_temp=76.0, comfort_heat=68.0, comfort_cool=76.0)
        engine._sensor_check_callback = lambda: True  # any_monitored_sensor_open() -> True

        classification = _make_cool_day_classification()
        asyncio.run(engine.apply_tou_precondition(classification, target=64.0, schedule_id="s3"))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c.args[1] == "set_temperature"]
        assert len(temp_calls) == 0

    def test_occupancy_away_redirects_instead_of_banking(self):
        """Issue #85 safety net still applies to TOU pre-conditioning — away mode redirects
        to setback rather than commanding the banking target."""
        engine = _make_engine(indoor_temp=76.0, comfort_heat=68.0, comfort_cool=76.0)
        engine._occupancy_mode = "away"
        classification = _make_cool_day_classification()

        asyncio.run(engine.apply_tou_precondition(classification, target=64.0, schedule_id="s4"))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c.args[1] == "set_temperature"]
        # Away-mode setback fires (not the 64.0 banking target).
        assert all(c.args[2].get("temperature") != 64.0 for c in temp_calls)

    def test_emits_tou_precondition_applied_event(self):
        engine = _make_engine(indoor_temp=76.0, comfort_heat=68.0, comfort_cool=76.0)
        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda event_type, payload: events.append((event_type, payload))
        classification = _make_cool_day_classification()

        asyncio.run(engine.apply_tou_precondition(classification, target=64.0, schedule_id="s5"))

        tou_events = [e for e in events if e[0] == "tou_precondition_applied"]
        assert len(tou_events) == 1
        payload = tou_events[0][1]
        assert payload["schedule_id"] == "s5"
        assert payload["target"] == 64.0
        assert payload["mode"] == "cool"

    def test_no_event_when_occupancy_redirects(self):
        """Away mode redirects the write — the "applied" event must not fire, since it
        would misleadingly imply the banking target was actually commanded."""
        engine = _make_engine(indoor_temp=76.0, comfort_heat=68.0, comfort_cool=76.0)
        engine._occupancy_mode = "away"
        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda event_type, payload: events.append((event_type, payload))
        classification = _make_cool_day_classification()

        asyncio.run(engine.apply_tou_precondition(classification, target=64.0, schedule_id="s6"))

        assert not any(e[0] == "tou_precondition_applied" for e in events)

    def test_skips_when_manual_override_active(self):
        """Issue #786 post-implementation audit, Fix 1: apply_tou_precondition() must defer
        to a protected manual override, exactly like handle_bedtime()/handle_morning_wakeup()/
        handle_pre_cool() already do via decide_scheduled_band_gate(). Before this fix, the
        function never checked _manual_override_active at all — it would silently overwrite
        a user's manual override with the TOU banking target."""
        engine = _make_engine(indoor_temp=76.0, comfort_heat=68.0, comfort_cool=76.0)
        engine._manual_override_active = True
        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda event_type, payload: events.append((event_type, payload))
        classification = _make_cool_day_classification()

        asyncio.run(engine.apply_tou_precondition(classification, target=64.0, schedule_id="s7"))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c.args[1] == "set_temperature"]
        assert len(temp_calls) == 0
        assert not any(e[0] == "tou_precondition_applied" for e in events)

    def test_skips_when_paused_by_door_flag_set(self):
        """Proves the new gate-based check reads the authoritative _paused_by_door flag
        directly (not only via the live-sensor callback the old bespoke check used)."""
        engine = _make_engine(indoor_temp=76.0, comfort_heat=68.0, comfort_cool=76.0)
        engine._paused_by_door = True
        classification = _make_cool_day_classification()

        asyncio.run(engine.apply_tou_precondition(classification, target=64.0, schedule_id="s8"))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c.args[1] == "set_temperature"]
        assert len(temp_calls) == 0

    def test_skips_and_no_event_when_whf_owns_hvac(self):
        """Closes Fix 2: when a WHF session owns HVAC, apply_tou_precondition() must return
        before ever reaching _set_temperature_for_mode() or the event-emission code — proving
        the misleading "applied" event during a WHF block cannot fire once Fix 1 is in place.
        _whf_owns_hvac() derives from fan_lifecycle_state, which requires fan_mode ==
        FAN_MODE_WHOLE_HOUSE (or BOTH) AND _pre_fan_hvac_mode is not None (an active
        suppression session) — see AutomationEngine._whf_owns_hvac()'s docstring."""
        engine = _make_engine(indoor_temp=76.0, comfort_heat=68.0, comfort_cool=76.0)
        engine.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        engine._pre_fan_hvac_mode = "cool"
        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda event_type, payload: events.append((event_type, payload))
        classification = _make_cool_day_classification()

        assert engine._whf_owns_hvac() is True

        asyncio.run(engine.apply_tou_precondition(classification, target=64.0, schedule_id="s9"))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c.args[1] == "set_temperature"]
        assert len(temp_calls) == 0
        assert not any(e[0] == "tou_precondition_applied" for e in events)

    def test_proceeds_normally_when_gate_returns_proceed(self):
        """Regression guard for this change: with no override/pause/nat-vent/WHF/occupancy
        state active, decide_scheduled_band_gate() returns PROCEED and the banking target is
        still commanded — confirming the gate-based replacement didn't break the happy path
        already covered by test_commands_target_override_not_plain_comfort_value."""
        engine = _make_engine(indoor_temp=76.0, comfort_heat=68.0, comfort_cool=76.0)
        classification = _make_cool_day_classification()

        asyncio.run(engine.apply_tou_precondition(classification, target=64.0, schedule_id="s10"))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c.args[1] == "set_temperature"]
        assert len(temp_calls) == 1
        payload = temp_calls[0].args[2]
        assert payload["temperature"] == 64.0
        assert payload["hvac_mode"] == "cool"

    def test_skips_when_override_confirm_pending(self):
        """Issue #786 post-implementation audit, Fix 2: during the confirm window between
        detecting a manual thermostat change and _confirm_override_action() promoting it to
        _manual_override_active, _manual_override_active is still False — so the
        decide_scheduled_band_gate() check alone lets TOU banking overwrite a not-yet-
        confirmed override. apply_classification() already guards this exact window via its
        own _override_confirm_pending check (automation.py:2458); apply_tou_precondition()
        must mirror it."""
        engine = _make_engine(indoor_temp=76.0, comfort_heat=68.0, comfort_cool=76.0)
        engine._manual_override_active = False
        engine._override_confirm_pending = True
        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda event_type, payload: events.append((event_type, payload))
        classification = _make_cool_day_classification()

        asyncio.run(engine.apply_tou_precondition(classification, target=64.0, schedule_id="s11"))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c.args[1] == "set_temperature"]
        assert len(temp_calls) == 0
        assert not any(e[0] == "tou_precondition_applied" for e in events)

    def test_banking_below_comfort_heat_does_not_trigger_false_incident(self):
        """Issue #786 post-implementation audit, Fix 3: TOU pre-conditioning intentionally
        banks a cool-mode setpoint down toward — or below — comfort_heat (e.g. a
        sleep_heat-derived target while the lead-time window overlaps the sleep schedule).
        The _set_temperature() sanity check that flags cool-mode-below-comfort_heat as a
        SETPOINT INCONSISTENCY must not fire for this intentional write; it must still fire
        for a normal (non-TOU) comfort-band write with the same inconsistent numbers — see
        test_normal_comfort_band_write_below_comfort_heat_still_flags_incident below."""
        engine = _make_engine(indoor_temp=76.0, comfort_heat=68.0, comfort_cool=76.0)
        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda event_type, payload: events.append((event_type, payload))
        classification = _make_cool_day_classification()

        # target=64.0 is below comfort_heat=68.0 — mirrors banking to a sleep_heat target.
        asyncio.run(engine.apply_tou_precondition(classification, target=64.0, schedule_id="s12"))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c.args[1] == "set_temperature"]
        assert len(temp_calls) == 1
        assert temp_calls[0].args[2]["temperature"] == 64.0
        assert not any(e[0] == "incident_detected" for e in events)

    def test_normal_comfort_band_write_below_comfort_heat_still_flags_incident(self):
        """Non-regression companion to the fix above: a normal (non-TOU) call to
        _set_temperature() with a cool-mode setpoint below comfort_heat must still emit
        incident_detected — the sanity check is only exempted for the TOU call site."""
        engine = _make_engine(indoor_temp=76.0, comfort_heat=68.0, comfort_cool=76.0)
        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda event_type, payload: events.append((event_type, payload))

        asyncio.run(engine._set_temperature(64.0, reason="test_direct_call", mode="cool"))

        incident_events = [e for e in events if e[0] == "incident_detected"]
        assert len(incident_events) == 1
        assert incident_events[0][1]["incident_class"] == "setpoint_mode_inconsistency"
