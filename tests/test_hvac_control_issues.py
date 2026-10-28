"""Status-tab ``hvac_control_issues`` list (Issue #1049, change 7).

Occupant: Status read "active" while the thermostat ignored the requested mode and the house
stayed cold. The coordinator now lists open mode-alignment / command-failure episodes next to
``invariant_violations``; the entry disappears when the episode recovers.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor import automation as _am  # noqa: E402
from tests.test_hvac_mode_alignment import NOW, _engine, _patched, _Timers  # noqa: E402

_PKG = Path(__file__).parent.parent / "custom_components" / "climate_advisor"


def _coord(engine, *, live=None):
    """Coordinator stub bound to a REAL AutomationEngine (no hand-set episode state)."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    coord = MagicMock()
    coord.automation_engine = engine
    coord.hass.states.get.side_effect = lambda _eid: engine.hass.states.get(_eid)
    coord._compute_hvac_control_issues = types.MethodType(
        mod.ClimateAdvisorCoordinator._compute_hvac_control_issues, coord
    )
    return coord


def _issues(coord, now):
    with patch("custom_components.climate_advisor.coordinator.dt_util") as dt:
        dt.now.return_value = now
        return coord._compute_hvac_control_issues()


def _open_mode_episode(engine, timers):
    """Drive the REAL engine to an open 'thermostat ignored the requested mode' episode."""
    asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
    timers.fire(engine, 10)
    timers.fire(engine, 130)
    assert engine._hvac_write_record.episode_reason == "heat"


def _open_failure_episode(engine):
    engine.hass.services.async_call.side_effect = RuntimeError("boom")
    asyncio.run(engine._set_hvac_mode("cool", reason="t"))
    assert engine._hvac_cmd_fail_announced is not None


class TestComputeHvacControlIssues:
    def test_mode_not_obeyed_listed_with_diagnostic_detail(self):
        engine = _engine("off")
        timers = _Timers()
        p1, p2, p3 = _patched(timers)
        with p1, p2, p3:
            _open_mode_episode(engine, timers)
        out = _issues(_coord(engine), NOW + timedelta(minutes=12))
        assert [i["issue"] for i in out] == ["mode_not_obeyed"]
        d = out[0]["detail"]
        assert "heat" in d and "'off'" in d and "12 min" in d

    def test_clears_when_episode_recovers(self):
        engine = _engine("off")
        timers = _Timers()
        p1, p2, p3 = _patched(timers)
        with p1, p2, p3:
            _open_mode_episode(engine, timers)
            engine.hass.states.get.return_value.state = "heat"
            timers.fire(engine, 130)
        assert _issues(_coord(engine), NOW) == []

    def test_command_failing_shows_the_attempted_mode_not_the_episode_key(self):
        engine = _engine("off")
        p1, p2, p3 = _patched(_Timers())
        with p1, p2, p3:
            _open_failure_episode(engine)
        out = _issues(_coord(engine), NOW + timedelta(minutes=5))
        assert [i["issue"] for i in out] == ["command_failing"]
        assert "last attempted mode: cool" in out[0]["detail"] and "5 min" in out[0]["detail"]
        assert "failed)" not in out[0]["detail"]

    def test_both_episodes_listed(self):
        engine = _engine("off")
        timers = _Timers()
        p1, p2, p3 = _patched(timers)
        with p1, p2, p3:
            _open_mode_episode(engine, timers)
            _open_failure_episode(engine)
        # Issue #1121: command_failing is held until the failure episode is >= 5 min old.
        assert {i["issue"] for i in _issues(_coord(engine), NOW + timedelta(minutes=5))} == {
            "mode_not_obeyed",
            "command_failing",
        }

    def test_unconfigured_mock_engine_yields_nothing(self):
        """A bare MagicMock engine (unset attributes are truthy) must not fabricate issues."""
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        coord = MagicMock()
        coord.automation_engine = MagicMock()
        coord._compute_hvac_control_issues = types.MethodType(
            mod.ClimateAdvisorCoordinator._compute_hvac_control_issues, coord
        )
        assert _issues(coord, datetime.now(UTC)) == []

    def test_detail_is_not_an_instruction(self):
        engine = _engine("off")
        timers = _Timers()
        p1, p2, p3 = _patched(timers)
        with p1, p2, p3:
            _open_mode_episode(engine, timers)
        d = _issues(_coord(engine), NOW)[0]["detail"].lower()
        for word in ("please", "you should", "turn ", "check "):
            assert word not in d


class TestCommandFailingBannerHold:
    """Issue #1121: one brief thermostat error must not raise a red "commands are failing"
    banner that contradicts a thermostat already doing the right thing."""

    def _failed(self, live="off"):
        engine = _engine(live)
        events = []
        engine._emit_event_callback = lambda n, d: events.append((n, d))
        p1, p2, p3 = _patched(_Timers())
        with p1, p2, p3:
            _open_failure_episode(engine)  # set_hvac_mode("cool") fails once
        engine.hass.services.async_call.side_effect = None
        return engine, events

    def test_hidden_for_the_first_five_minutes(self):
        engine, _ = self._failed()
        coord = _coord(engine)
        assert _issues(coord, NOW) == []
        assert _issues(coord, NOW + timedelta(minutes=4, seconds=59)) == []
        assert [i["issue"] for i in _issues(coord, NOW + timedelta(minutes=5))] == ["command_failing"]

    def test_incident_still_announced_immediately_while_banner_held(self):
        engine, events = self._failed()
        assert [d["incident_class"] for n, d in events if n == "incident_detected"] == ["hvac_command_failed"]
        assert _issues(_coord(engine), NOW) == []

    def test_hidden_when_thermostat_already_reports_the_failed_mode(self):
        engine, _ = self._failed()
        engine.hass.states.get.return_value.state = "cool"
        assert _issues(_coord(engine), NOW + timedelta(minutes=30)) == []

    def test_hidden_while_user_owns_the_thermostat(self):
        for attr in ("_manual_override_active", "_override_confirm_pending"):
            engine, _ = self._failed()
            setattr(engine, attr, True)
            assert _issues(_coord(engine), NOW + timedelta(minutes=30)) == [], attr

    def test_successful_nudge_closes_the_failure_episode(self):
        """Mirror: the episode opens on any failed climate write (including a nudge), so a
        successful nudge closes it like every other successful climate write."""
        from tests.test_hvac_command_visibility import _run_scheduled

        engine = _engine("heat", temperature=68.0)  # reports 68F vs 72F commanded
        events = []
        engine._emit_event_callback = lambda n, d: events.append((n, d))
        captured: list = []
        engine._setpoint_reject_streak = 1  # next rejection is the 2nd -> nudge
        with (
            patch.object(_am, "async_call_later", side_effect=lambda h, d, cb: captured.append((d, cb))),
            patch.object(_am, "callback", side_effect=lambda fn: fn),
        ):
            asyncio.run(engine._set_temperature(72.0, reason="t", mode="heat"))
            _run_scheduled(engine, captured, 10)  # setpoint check: rejected -> retry in 900 s
            engine._note_hvac_command_failed("heat", RuntimeError("cloud hiccup"))
            assert engine._hvac_cmd_fail_announced is not None
            _run_scheduled(engine, captured, 900)  # nudge write succeeds
        assert any(n == "setpoint_nudge" for n, _ in events)
        assert engine._hvac_cmd_fail_announced is None
        assert [n for n, _ in events if n == "hvac_command_recovered"] == ["hvac_command_recovered"]
        assert _issues(_coord(engine), NOW + timedelta(minutes=30)) == []


class TestStatusPathWiring:
    def test_not_a_string_in_automation_status(self):
        src = (_PKG / "coordinator.py").read_text(encoding="utf-8")
        body = src.split("def _compute_automation_status", 1)[1].split("\n    def ", 1)[0]
        assert "hvac_control_issues" not in body and "_compute_hvac_control_issues" not in body

    def test_coordinator_data_exposes_key(self):
        src = (_PKG / "coordinator.py").read_text(encoding="utf-8")
        assert '"hvac_control_issues": _hvac_control_issues' in src

    def test_frontend_reads_exact_key_with_single_renderer(self):
        html = (_PKG / "frontend" / "index.html").read_text(encoding="utf-8")
        assert html.count("data.hvac_control_issues") == 1
        assert '(data.hvac_control_issues || []).map(v => `<div class="value"' in html
        assert "escapeHtml(v.detail)" in html
