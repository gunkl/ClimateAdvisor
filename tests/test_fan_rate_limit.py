"""Tests for Issue #641: hard safety floor on CA-issued fan toggle frequency.

Defense-in-depth backstop, independent of the specific root cause fixed elsewhere
(see tests/test_nat_vent_activation.py::TestIssue641ExitReactivationFlipFlop) —
_activate_fan()/_deactivate_fan() must refuse to reverse the fan's state within
FAN_MIN_TOGGLE_INTERVAL_S (300s) of CA's own last command, regardless of which
upstream decision logic asked for the reversal.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.const import (
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    FAN_MIN_TOGGLE_INTERVAL_S,
    FAN_MODE_WHOLE_HOUSE,
)

_DT_NOW_PATH = "custom_components.climate_advisor.automation.dt_util.now"


def _consume_coroutine(coro):
    coro.close()


def _make_engine() -> AutomationEngine:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    config = {
        "comfort_heat": 68.0,
        "comfort_cool": 74.0,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
        CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
        CONF_FAN_ENTITY: "fan.attic",
    }
    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service=config["notify_service"],
        config=config,
    )
    return engine


def _get_service_calls(engine, domain: str, service: str) -> list:
    return [c for c in engine.hass.services.async_call.call_args_list if c[0][0] == domain and c[0][1] == service]


class TestFanToggleRateLimitSuppression:
    """A reversal within the cooldown window must be suppressed, logged, and
    flagged as a fan_rapid_cycling incident — never silent."""

    def test_reactivate_within_window_is_suppressed(self):
        """Fan turned on, legitimately deactivated after the cooldown has elapsed
        (so the deactivate itself isn't also caught by the guard), then a too-soon
        reactivate attempt must no-op — the exact WHF fast-cycling symptom,
        independent of whatever upstream logic asked for the reversal."""
        engine = _make_engine()
        t0 = datetime(2026, 8, 15, 6, 36, 0)
        with patch(_DT_NOW_PATH, return_value=t0):
            asyncio.run(engine._activate_fan(reason="initial activation"))
        assert engine._fan_active is True
        assert len(_get_service_calls(engine, "fan", "turn_on")) == 1

        # Deactivate after the cooldown has elapsed — a legitimate, correctly-spaced
        # exit, not itself subject to the guard.
        t1 = t0 + timedelta(seconds=FAN_MIN_TOGGLE_INTERVAL_S + 1)
        with patch(_DT_NOW_PATH, return_value=t1):
            asyncio.run(engine._deactivate_fan(reason="exit"))
        assert engine._fan_active is False
        assert len(_get_service_calls(engine, "fan", "turn_off")) == 1

        # Reactivate 10s after the deactivate — well within the 300s floor — must
        # be suppressed (this is the reported incident's actual sequence).
        t2 = t1 + timedelta(seconds=10)
        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))
        with patch(_DT_NOW_PATH, return_value=t2), patch("custom_components.climate_advisor.automation._LOGGER") as log:
            asyncio.run(engine._activate_fan(reason="reactivation attempt"))

        assert engine._fan_active is False, "rate limit must suppress the reactivation"
        assert len(_get_service_calls(engine, "fan", "turn_on")) == 1, "no second turn_on call issued"
        log.warning.assert_called_once()
        assert "rate limit" in log.warning.call_args.args[0].lower()

        incidents = [e for e in events if e[0] == "incident_detected"]
        assert len(incidents) == 1
        assert incidents[0][1]["incident_class"] == "fan_rapid_cycling"
        assert incidents[0][1]["attempted_action"] == "activate"
        assert incidents[0][1]["min_interval_s"] == FAN_MIN_TOGGLE_INTERVAL_S

    def test_deactivate_within_window_is_suppressed(self):
        """Symmetric case: activate, then a too-soon deactivate attempt is suppressed."""
        engine = _make_engine()
        t0 = datetime(2026, 8, 15, 6, 36, 0)
        with patch(_DT_NOW_PATH, return_value=t0):
            asyncio.run(engine._activate_fan(reason="initial activation"))
        assert engine._fan_active is True

        t1 = t0 + timedelta(seconds=30)
        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))
        with patch(_DT_NOW_PATH, return_value=t1):
            asyncio.run(engine._deactivate_fan(reason="too-soon exit attempt"))

        assert engine._fan_active is True, "rate limit must suppress the too-soon deactivation"
        assert len(_get_service_calls(engine, "fan", "turn_off")) == 0
        incidents = [e for e in events if e[0] == "incident_detected"]
        assert incidents[0][1]["attempted_action"] == "deactivate"

    def test_suppression_sets_fan_rate_limited_until(self):
        """_fan_rate_limited_until is set to command_time + interval — the field the
        status-tab suffix (_whf_rate_limit_suffix) reads."""
        engine = _make_engine()
        t0 = datetime(2026, 8, 15, 6, 36, 0)
        with patch(_DT_NOW_PATH, return_value=t0):
            asyncio.run(engine._activate_fan(reason="initial activation"))

        t1 = t0 + timedelta(seconds=1)
        with patch(_DT_NOW_PATH, return_value=t1):
            asyncio.run(engine._deactivate_fan(reason="suppressed"))

        assert engine._fan_rate_limited_until == t0 + timedelta(seconds=FAN_MIN_TOGGLE_INTERVAL_S)


class TestFanToggleRateLimitAllowsSpacedToggles:
    """A toggle spaced >= 300s from the last command must NOT be throttled — this
    guard must never suppress ordinary, correctly-spaced fan cycling."""

    def test_reactivate_after_window_elapses_is_allowed(self):
        engine = _make_engine()
        t0 = datetime(2026, 8, 15, 6, 36, 0)
        with patch(_DT_NOW_PATH, return_value=t0):
            asyncio.run(engine._activate_fan(reason="initial activation"))

        t1 = t0 + timedelta(seconds=FAN_MIN_TOGGLE_INTERVAL_S + 1)
        with patch(_DT_NOW_PATH, return_value=t1):
            asyncio.run(engine._deactivate_fan(reason="exit"))
        assert engine._fan_active is False

        t2 = t1 + timedelta(seconds=FAN_MIN_TOGGLE_INTERVAL_S + 1)
        with patch(_DT_NOW_PATH, return_value=t2):
            asyncio.run(engine._activate_fan(reason="legitimate reactivation"))

        assert engine._fan_active is True
        assert len(_get_service_calls(engine, "fan", "turn_on")) == 2

    def test_first_ever_activation_is_never_rate_limited(self):
        """_fan_command_time is None before any CA command — must never be treated as
        'elapsed < window' (that would permanently block the very first activation)."""
        engine = _make_engine()
        assert engine._fan_command_time is None
        asyncio.run(engine._activate_fan(reason="first activation"))
        assert engine._fan_active is True
        assert len(_get_service_calls(engine, "fan", "turn_on")) == 1


class TestFanToggleRateLimitNeverBlocksManualOverride:
    """A genuine user/RF-remote fan action must never be throttled — both
    _activate_fan/_deactivate_fan already return before reaching the rate-limit
    check when an override is active, so CA's own rate limiter is structurally
    inert for manual actions."""

    def test_override_active_skips_rate_limit_check_entirely(self):
        engine = _make_engine()
        t0 = datetime(2026, 8, 15, 6, 36, 0)
        with patch(_DT_NOW_PATH, return_value=t0):
            asyncio.run(engine._activate_fan(reason="initial activation"))

        engine._fan_override_active = True
        t1 = t0 + timedelta(seconds=1)  # well within the cooldown window
        with (
            patch(_DT_NOW_PATH, return_value=t1),
            patch.object(engine, "_fan_toggle_rate_limited") as rate_limit_spy,
        ):
            asyncio.run(engine._deactivate_fan(reason="manual override active"))

        rate_limit_spy.assert_not_called()


class TestFanToggleRateLimitIgnoresBookkeepingOnlyStamps:
    """Found and fixed during this issue's own implementation:
    _reconcile_fan_physical_drift()'s Issue #449/#482 corrective "sync the stuck
    control entity" command stamps the shared _fan_command_time echo-tracking field
    (used for provenance/manual-vs-CA attribution) without representing a real
    physical toggle — the fan was already off; only the control entity's stale
    belief changes. That method's own docstring documents an immediately-following
    same-tick recycle-on as correct, expected behavior. _fan_toggle_rate_limited()
    must key off the separate _fan_toggle_command_time field (stamped only by
    _activate_fan/_deactivate_fan's own real command sites) so this legitimate
    reconcile-then-recycle sequence is never mistaken for the flip-flop this guard
    exists to catch."""

    def test_bookkeeping_only_command_time_stamp_does_not_block_real_toggle(self):
        engine = _make_engine()
        now = datetime(2026, 8, 15, 6, 40, 0)
        # Simulate a bookkeeping-only stamp (e.g. the drift-reconciliation echo
        # command) WITHOUT going through _activate_fan/_deactivate_fan — mirrors
        # automation.py's _reconcile_fan_physical_drift() stamping _fan_command_time
        # directly, right before its own _command_whf_control_entity() call.
        engine._fan_command_time = now
        assert engine._fan_toggle_command_time is None

        with patch(_DT_NOW_PATH, return_value=now):
            asyncio.run(engine._activate_fan(reason="nat_vent_cycling_on"))

        assert engine._fan_active is True, (
            "a bookkeeping-only _fan_command_time stamp (no prior real toggle) must not "
            "block the very next real _activate_fan() call, even at the same instant"
        )
        assert len(_get_service_calls(engine, "fan", "turn_on")) == 1
