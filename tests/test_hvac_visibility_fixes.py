"""Verifier fixes for the #1048 visibility work (Issue #1049, Phase 1 follow-up).

Real engine / real coordinator methods throughout (no hand-set episode state):
 * episode wording uses the attempted mode, not the episode key
 * an open "mode not applied" episode closes when the occupant takes over or the thermostat
   already reports the requested mode (and Status never shows a self-contradicting entry)
 * the setpoint write's real outcome reaches incident context and diagnostics
 * the Status refresh callback fires once per episode open / recovery
 * redaction works inside strings; pre-cool Status text; setpoint-override semantics
 * the harness's comfort_band_armed requires a band that was actually sent
"""

from __future__ import annotations

import asyncio
import types
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor import automation as automation_module
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.diagnostic_snapshot import REDACTED, recent_hvac_events, redact_sensitive
from custom_components.climate_advisor.diagnostic_snapshot import _build_last_command as build_last_command
from tests.test_heat_cool_override import (
    _NOW_BASE,
    _PATCH_CALLBACK,
    _PATCH_DT_UTIL,
    _make_coordinator_stub,
    _make_event,
)
from tests.test_hvac_mode_alignment import NOW, _engine, _patched, _Timers
from tools.sim_harness.outcomes import check_assertion


def _status_coord(engine):
    coord = MagicMock()
    coord.automation_engine = engine
    coord.hass.states.get.side_effect = lambda eid: engine.hass.states.get(eid)
    coord._compute_hvac_control_issues = types.MethodType(ClimateAdvisorCoordinator._compute_hvac_control_issues, coord)
    return coord


def _issues(coord, now=NOW):
    with patch("custom_components.climate_advisor.coordinator.dt_util") as dt:
        dt.now.return_value = now
        return coord._compute_hvac_control_issues()


def _open_episode(engine, timers):
    asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
    timers.fire(engine, 10)
    timers.fire(engine, 130)
    assert engine._hvac_write_record.episode_reason == "heat"


class TestEpisodeClosesWhenRealityOvertakesIt:
    def test_a_occupant_takes_manual_control_closes_the_episode_quietly(self):
        engine = _engine("off")
        timers = _Timers()
        p1, p2, p3 = _patched(timers)
        with p1, p2, p3:
            _open_episode(engine, timers)
            assert [i["issue"] for i in _issues(_status_coord(engine))] == ["mode_not_obeyed"]
            engine._manual_override_active = True  # occupant switched the thermostat themselves
            timers.fire(engine, 130)  # every later check is excluded -- must still clear the warning
        assert engine._hvac_write_record.episode_reason is None
        assert _issues(_status_coord(engine)) == []
        # Quiet: no false "the thermostat is now in heat" recovery claim.
        assert "hvac_mode_applied_recovered" not in [n for n, _ in engine._events]

    def test_a2_override_beginning_closes_it_immediately_via_the_real_entry_point(self):
        engine = _engine("off")
        timers = _Timers()
        p1, p2, p3 = _patched(timers)
        with p1, p2, p3:
            _open_episode(engine, timers)
            real = automation_module.AutomationEngine.start_override_confirmation
            engine.start_override_confirmation = types.MethodType(real, engine)
            engine._resolve_override_grace_fsm_state = MagicMock()
            try:
                from custom_components.climate_advisor.override_grace_fsm import OverrideGraceFsmEventKind as K

                engine.start_override_confirmation(source="normal", event_kind=K.OVERRIDE_DETECTED)
            except Exception:  # noqa: BLE001 -- only the leading hook matters here
                pass
        assert engine._hvac_write_record.episode_reason is None

    def test_b_occupant_sets_the_requested_mode_themselves(self):
        engine = _engine("off")
        timers = _Timers()
        p1, p2, p3 = _patched(timers)
        with p1, p2, p3:
            _open_episode(engine, timers)
            engine.hass.states.get.return_value.state = "heat"  # occupant (not an override) sets heat
            # Status never shows a contradicting 'requested heat, reports heat' even before the engine reconciles.
            assert _issues(_status_coord(engine)) == []
            engine.reconcile_mode_episode()
        assert engine._hvac_write_record.episode_reason is None
        names = [n for n, _ in engine._events]
        assert names.count("hvac_mode_applied_recovered") == 1  # it IS true: the thermostat is in heat

    def test_status_hidden_while_the_occupant_owns_the_thermostat(self):
        engine = _engine("off")
        timers = _Timers()
        p1, p2, p3 = _patched(timers)
        with p1, p2, p3:
            _open_episode(engine, timers)
            engine._override_confirm_pending = True
            assert _issues(_status_coord(engine)) == []

    def test_reconcile_with_no_open_episode_is_a_noop(self):
        engine = _engine("off")
        engine.reconcile_mode_episode()
        assert engine._events == []


class TestAttemptedModeWording:
    def test_command_failing_uses_the_attempted_mode_and_diagnostics_agree(self):
        engine = _engine("off")
        engine.hass.services.async_call.side_effect = RuntimeError("boom")
        with patch.object(automation_module.dt_util, "now", return_value=NOW):
            asyncio.run(engine._set_hvac_mode("cool", reason="t"))
        assert engine._hvac_cmd_fail_mode == "cool"
        assert engine._hvac_cmd_fail_announced == "failed"
        # Issue #1121: the entry is held until the failure episode is 5 min old.
        detail = _issues(_status_coord(engine), NOW + timedelta(minutes=5))[0]["detail"]
        assert "last attempted mode: cool" in detail
        snap = build_last_command(SimpleNamespace(automation_engine=engine), "off", "fahrenheit")
        assert snap["command_failure_mode"] == "cool"

    def test_attempted_mode_cleared_on_recovery(self):
        engine = _engine("off")
        engine.hass.services.async_call.side_effect = RuntimeError("boom")
        asyncio.run(engine._set_hvac_mode("cool", reason="t"))
        engine.hass.services.async_call.side_effect = None
        asyncio.run(engine._set_hvac_mode("cool", reason="t"))
        assert engine._hvac_cmd_fail_mode is None


class TestSetpointWriteOutcomeIsRecorded:
    def _ctx(self, engine):
        coord = MagicMock()
        coord.automation_engine = engine
        coord.hass.states.get.side_effect = lambda eid: engine.hass.states.get(eid)
        coord._thermostat_incident_context = types.MethodType(
            ClimateAdvisorCoordinator._thermostat_incident_context, coord
        )
        return coord._thermostat_incident_context()

    def test_setpoint_raise_while_already_in_heat_reports_failed_not_sent(self):
        engine = _engine("heat")  # already in the commanded mode: only the setpoint call runs
        engine.hass.services.async_call.side_effect = RuntimeError("boom")
        asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
        assert self._ctx(engine)["last_command_result"] == "failed"
        snap = build_last_command(SimpleNamespace(automation_engine=engine), "heat", "fahrenheit")
        assert snap["result"] == "failed"

    @pytest.mark.parametrize(
        ("setup", "expected"),
        [
            (lambda e: setattr(e, "_whf_owns_hvac", lambda: True), "blocked"),
            (lambda e: setattr(e, "dry_run", True), "dry_run"),
            (lambda e: None, "sent"),
        ],
    )
    def test_other_setpoint_outcomes(self, setup, expected):
        engine = _engine("heat")
        engine._last_commanded_hvac_mode = "heat"  # an earlier command, so the context has a "last command"
        setup(engine)
        asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
        assert self._ctx(engine)["last_command_result"] == expected


class TestStatusRefreshOncePerEpisode:
    def test_mode_episode_open_and_recovery_request_one_refresh_each(self):
        engine = _engine("off")
        engine._request_refresh_callback = MagicMock()
        timers = _Timers()
        p1, p2, p3 = _patched(timers)
        with p1, p2, p3:
            asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
            timers.fire(engine, 10)
            engine._request_refresh_callback.assert_not_called()  # early check only starts the clock
            timers.fire(engine, 130)
            assert engine._request_refresh_callback.call_count == 1  # open
            timers.fire(engine, 130)
            timers.fire(engine, 130)
            assert engine._request_refresh_callback.call_count == 1  # repeats in the episode: silent
            engine.hass.states.get.return_value.state = "heat"
            timers.fire(engine, 130)
            assert engine._request_refresh_callback.call_count == 2  # recovery
            timers.fire(engine, 130)
            assert engine._request_refresh_callback.call_count == 2

    def test_command_failure_episode_open_and_recovery_request_one_refresh_each(self):
        engine = _engine("heat")
        engine._request_refresh_callback = MagicMock()
        engine.hass.services.async_call.side_effect = RuntimeError("boom")
        for _ in range(3):
            asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
        assert engine._request_refresh_callback.call_count == 1
        engine.hass.services.async_call.side_effect = None
        asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
        assert engine._request_refresh_callback.call_count == 2
        asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
        assert engine._request_refresh_callback.call_count == 2


class TestRedactionInsideStrings:
    @pytest.mark.parametrize(
        "text",
        [
            "Cannot connect to host 192.168.1.40:80 ssl:default",
            "192.168.1.40:80",
            "192.168.1.40",
            "timeout talking to 10.0.0.5",
            "device aa:bb:cc:dd:ee:ff went offline",
        ],
    )
    def test_addresses_inside_strings_are_redacted(self, text):
        out = redact_sensitive({"error": text})["error"]
        assert "192.168" not in out and "10.0.0.5" not in out and "aa:bb" not in out
        assert REDACTED in out

    def test_surrounding_text_is_kept(self):
        out = redact_sensitive({"error": "Cannot connect to host 192.168.1.40:80 ssl:default"})["error"]
        assert out == f"Cannot connect to host {REDACTED} ssl:default"

    def test_version_named_keys_keep_dotted_versions_but_never_macs(self):
        out = redact_sensitive({"firmware_version": "1.2.3.4", "sw_version": "v2.0.1.7 build"})
        assert out == {"firmware_version": "1.2.3.4", "sw_version": "v2.0.1.7 build"}
        assert REDACTED in redact_sensitive({"firmware_version": "aa:bb:cc:dd:ee:ff"})["firmware_version"]

    def test_non_address_numbers_untouched(self):
        assert redact_sensitive({"note": "setpoint 72.5 at 3 points", "ratio": "1.2.3"}) == {
            "note": "setpoint 72.5 at 3 points",
            "ratio": "1.2.3",
        }
        assert redact_sensitive({"x": "999.1.1.1"}) == {"x": "999.1.1.1"}  # not a valid octet set

    @pytest.mark.parametrize("key", ["name", "Name", "owner", "devicename", "DeviceName", "device_name"])
    def test_identifying_name_keys_redacted(self, key):
        assert redact_sensitive({key: "Alice's Thermostat"}) == {key: REDACTED}

    def test_recent_hvac_event_error_strings_are_redacted(self):
        coord = SimpleNamespace(
            _event_log=[
                {
                    "type": "incident_detected",
                    "incident_class": "hvac_command_failed",
                    "error": "Cannot connect to host 192.168.1.40:80 ssl:default",
                }
            ]
        )
        assert "192.168" not in repr(recent_hvac_events(coord))


class TestPreCoolStatusText:
    def _run(self, result):
        coord = MagicMock()
        coord.automation_engine = SimpleNamespace(
            natural_vent_active=False, handle_pre_cool=AsyncMock(return_value=result)
        )
        coord._get_indoor_temp = MagicMock(return_value=76.0)
        coord.async_refresh = AsyncMock()
        coord._pre_cool_trigger_dt = object()
        asyncio.run(ClimateAdvisorCoordinator._async_pre_cool_trigger(coord, NOW))
        return coord._pre_cool_status

    def test_not_applied_is_not_mangled_into_an_active_status(self):
        status = self._run("not applied (blocked): 72.0°F")
        assert status == "pre-cool not applied (blocked): 72.0°F"
        assert not status.endswith(")") or "(blocked)" in status

    def test_applied_and_suppressed_unchanged(self):
        assert self._run("applied: 72.0°F") == "pre-cool active (72.0°F)"
        assert self._run("suppressed: nat-vent achieved 70.0°F").startswith("pre-cool suppressed")


def _run_thermostat_event(coord, event):
    mock_dt = MagicMock()
    mock_dt.now.return_value = _NOW_BASE
    with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
        asyncio.run(coord._async_thermostat_changed(event))


class TestSetpointOverrideSemanticsUnchangedOutsideRecovery:
    def test_missing_prior_setpoint_without_unavailable_keeps_override_semantics(self):
        """Only an availability recovery is exempt; an old setpoint of None otherwise behaves as before."""
        coord = _make_coordinator_stub()
        _run_thermostat_event(coord, _make_event(old_hvac_mode="cool", new_hvac_mode="cool", new_temp=72.0))
        assert coord.automation_engine.handle_manual_override.call_count == 1

    def test_fan_mode_branch_is_suppressed_by_the_setpoint_override_as_before(self):
        """A single event changing setpoint (from None) and fan_mode has ONE root cause: the setpoint."""
        coord = _make_coordinator_stub()
        _run_thermostat_event(
            coord,
            _make_event(
                old_hvac_mode="cool",
                new_hvac_mode="cool",
                new_temp=72.0,
                old_fan_mode="auto",
                new_fan_mode="on",
            ),
        )
        assert coord.automation_engine.handle_fan_manual_override.call_count == 0

    def test_recovery_still_exempt(self):
        coord = _make_coordinator_stub()
        _run_thermostat_event(coord, _make_event(old_hvac_mode="unavailable", new_hvac_mode="cool", new_temp=72.0))
        assert coord.automation_engine.handle_manual_override.call_count == 0


class TestComfortBandArmedRequiresASentBand:
    def _result(self, *events):
        return SimpleNamespace(
            event_log=[(t, p, NOW) for t, p in events],
            engine_state={},
            action_log=[],
            callback_errors=[],
            decisions=[],
        )

    def _check(self, payload, expect="comfort_band_armed", **extra):
        result = self._result(("comfort_band_applied", payload))
        a = {"at": "2026-01-12T23:00:00", "expect": expect, **extra}
        return check_assertion(result, a, decisions=[])

    @pytest.mark.parametrize("outcome", ["blocked", "failed", "dry_run", "superseded", "not_sent"])
    def test_unsent_band_is_not_armed(self, outcome):
        assert self._check({"floor": 70, "ceiling": 75, "outcome": outcome}) is False

    @pytest.mark.parametrize("payload", [{"floor": 70, "ceiling": 75, "outcome": "sent"}, {"floor": 70, "ceiling": 75}])
    def test_sent_and_legacy_events_count(self, payload):
        assert self._check(payload) == "comfort_band_armed"

    def test_expect_band_ignores_unsent_events(self):
        spec = {"expect_band": {"floor": 70, "ceiling": 75}}
        assert self._check({"floor": 70, "ceiling": 75, "outcome": "blocked"}, expect="expect_band", **spec) is False
        assert self._check({"floor": 70, "ceiling": 75, "outcome": "sent"}, expect="expect_band", **spec)
