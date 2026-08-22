"""Tests for Issue #729's two smaller, bundled fixes found during the
reload-based-promotion redesign:

1. Three ``AutomationEngine`` timers (the setpoint-retry chain inside
   ``_set_temperature()``, and the two post-fan setpoint-verify timers) were
   scheduled via ``async_call_later()`` but never tracked in an instance
   attribute ``cleanup()`` could reach — a real gap, since one of them
   (``_schedule_real_target``) can issue a real ``_set_temperature()`` call.
2. None of the 5 real-command chokepoints logged which engine (production vs.
   shadow) issued/would have issued a command — closed by interpolating
   ``self.role`` into each log line.
"""

from __future__ import annotations

import asyncio
import logging

from custom_components.climate_advisor.automation import AutomationEngine
from tools.sim_harness.build_engine import build_headless_engine


def _run(coro):
    return asyncio.run(coro)


class TestPreviouslyUncoveredTimersAreCancelled:
    """Recheck §1: these 3 cancel-handle attributes must be reachable from
    cleanup() — the actual regression proof, not just that the attribute
    exists."""

    def test_setpoint_retry_cancel_is_invoked_by_cleanup(self):
        engine, _, _, _ = build_headless_engine()
        calls: list[str] = []
        engine._setpoint_retry_cancel = lambda: calls.append("cancelled")
        engine.cleanup()
        assert calls == ["cancelled"]
        assert engine._setpoint_retry_cancel is None

    def test_fan_on_verify_cancel_is_invoked_by_cleanup(self):
        engine, _, _, _ = build_headless_engine()
        calls: list[str] = []
        engine._fan_on_verify_cancel = lambda: calls.append("cancelled")
        engine.cleanup()
        assert calls == ["cancelled"]
        assert engine._fan_on_verify_cancel is None

    def test_fan_off_verify_cancel_is_invoked_by_cleanup(self):
        engine, _, _, _ = build_headless_engine()
        calls: list[str] = []
        engine._fan_off_verify_cancel = lambda: calls.append("cancelled")
        engine.cleanup()
        assert calls == ["cancelled"]
        assert engine._fan_off_verify_cancel is None

    def test_cleanup_is_safe_with_no_pending_timers(self):
        engine, _, _, _ = build_headless_engine()
        engine.cleanup()  # must not raise — all 3 default to None


class TestRoleTagInCommandLogs:
    """Confirms the class-level default (so partially-constructed test fixtures
    don't crash) and that the tag actually appears in log output for both
    engine roles."""

    def test_role_defaults_via_class_attribute_for_partial_construction(self):
        partial = object.__new__(AutomationEngine)
        assert partial.role == "production"

    def test_set_hvac_mode_dry_run_logs_production_role(self, caplog):
        engine, _, _, _ = build_headless_engine(role="production", dry_run=True)
        with caplog.at_level(logging.INFO):
            _run(engine._set_hvac_mode("cool", reason="test"))
        assert any("role=production" in r.message for r in caplog.records)

    def test_set_hvac_mode_dry_run_logs_shadow_role(self, caplog):
        engine, _, _, _ = build_headless_engine(role="shadow", dry_run=True)
        with caplog.at_level(logging.INFO):
            _run(engine._set_hvac_mode("cool", reason="test"))
        assert any("role=shadow" in r.message for r in caplog.records)

    def test_activate_fan_dry_run_logs_role(self, caplog):
        engine, _, _, _ = build_headless_engine(config={"fan_mode": "whole_house_fan"}, role="shadow", dry_run=True)
        with caplog.at_level(logging.INFO):
            _run(engine._activate_fan(reason="test"))
        assert any("role=shadow" in r.message for r in caplog.records)

    def test_deactivate_fan_dry_run_logs_role(self, caplog):
        engine, _, _, _ = build_headless_engine(config={"fan_mode": "whole_house_fan"}, role="shadow", dry_run=True)
        engine._fan_active = True  # otherwise _deactivate_fan short-circuits as a no-op
        with caplog.at_level(logging.INFO):
            _run(engine._deactivate_fan(reason="test"))
        assert any("role=shadow" in r.message for r in caplog.records)
