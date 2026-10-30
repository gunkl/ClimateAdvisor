"""Production wiring of the household promises (Issue #1128, Step 3 / I8, log-only).

Drives the REAL coordinator and engine methods (partial instantiation, per CLAUDE.md) and asserts
outcomes: a promise miss is logged and journalled but never alerted; the AC/WHF mutex still alerts;
every command that really goes out is journalled; a journal fault never blocks a command.
Also settles Issue #1129 (fan reconcile path sends no Context) with a driven echo test.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import logging
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

from custom_components.climate_advisor.const import FAN_MODE_WHOLE_HOUSE  # noqa: E402
from custom_components.climate_advisor.promise_journal import PromiseJournal  # noqa: E402

NOW = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
COORD_MOD = "custom_components.climate_advisor.coordinator"

_BOUND = (
    "_promise_reading_now",
    "_record_promise_reading",
    "_record_promise_event",
    "_promise_observe_command",
    "_tick_household_promises",
    "_evaluate_household_promises",
    "_run_invariant_watchdog",
    "_build_state_dict",
    "_async_command_fan_entity",
    "_async_fan_entity_changed",
    "_fan_state_feedback_enabled",
)


def _coordinator_class():
    return importlib.import_module(COORD_MOD).ClimateAdvisorCoordinator


def _make_coord(*, automation_enabled=True, unit="fahrenheit", hvac_action="idle", fan_on=None, feedback=False):
    cls = _coordinator_class()
    c = object.__new__(cls)
    c.config = {
        "climate_entity": "climate.t",
        "temp_unit": unit,
        "fan_mode": FAN_MODE_WHOLE_HOUSE,
        "fan_entity": "switch.whf",
        "fan_state_feedback": feedback,
    }
    c.hass = MagicMock()
    climate = SimpleNamespace(state="cool", attributes={"hvac_action": hvac_action, "temperature": 74.0})
    c.hass.states.get = lambda eid: climate if eid == "climate.t" else None
    c.hass.services.async_call = AsyncMock()
    c._promise_journal = PromiseJournal()
    c._promise_stats = {}
    c._promise_armed_logged = False
    c._current_classification = SimpleNamespace(today_high=80.0)
    c._automation_enabled = automation_enabled
    c._get_indoor_temp = lambda: 70.0
    c._get_fan_physical_state = lambda: fan_on
    c._last_outdoor_temp = 80.0
    c._emit_event = MagicMock()
    c._notify_in_background = MagicMock()
    c._spawn = MagicMock(side_effect=lambda coro, **kw: coro.close())
    c._async_save_state = MagicMock(return_value=MagicMock(close=MagicMock()))
    c.automation_engine = MagicMock()
    c.automation_engine._mode_change_reason = "other"
    for name in _BOUND:
        setattr(c, name, types.MethodType(getattr(cls, name), c))
    return c


def _now_patch():
    return patch(f"{COORD_MOD}.dt_util.now", return_value=NOW)


class TestPromiseMissIsLoggedNotAlerted:
    def test_p3_violation_is_logged_journalled_and_not_notified(self, caplog):
        """A command sent while automation was off: the occupant is not paged, but it is on record."""
        c = _make_coord(automation_enabled=False)
        with _now_patch(), caplog.at_level(logging.INFO):
            c._promise_observe_command("climate", "set_hvac_mode", {"hvac_mode": "off"}, "climate")
            reported = c._evaluate_household_promises()
        assert [h.promise for h in reported] == ["observe_only_silence"]
        c._emit_event.assert_called_once()
        event_type, payload = c._emit_event.call_args.args
        assert event_type == "invariant_violation"
        assert payload["invariant"] == "observe_only_silence"
        assert payload["severity"] == "log" and payload["promise"] == "3"
        c._notify_in_background.assert_not_called()
        assert any(
            "Household promise missed (log only): promise=observe_only_silence" in r.message for r in caplog.records
        )
        assert c._promise_stats["observe_only_silence"]["hits"] == 1

    def test_same_occurrence_is_reported_once_across_ticks(self):
        c = _make_coord(automation_enabled=False)
        with _now_patch():
            c._promise_observe_command("climate", "set_hvac_mode", {"hvac_mode": "off"}, "climate")
            c._evaluate_household_promises()
            second = c._evaluate_household_promises()
        assert second == []
        assert c._emit_event.call_count == 1
        assert c._promise_stats["observe_only_silence"]["hits"] == 1

    def test_hit_requests_a_save_and_clean_run_does_not(self):
        c = _make_coord()
        with _now_patch():
            c._evaluate_household_promises()
        c._spawn.assert_not_called()
        c = _make_coord(automation_enabled=False)
        with _now_patch():
            c._promise_observe_command("fan", "turn_off", {"entity_id": "switch.whf"}, "fan")
            c._evaluate_household_promises()
        c._spawn.assert_called_once()

    def test_armed_line_is_logged_once(self, caplog):
        c = _make_coord()
        with _now_patch(), caplog.at_level(logging.INFO):
            c._evaluate_household_promises()
            c._evaluate_household_promises()
        assert sum("Household promises armed" in r.message for r in caplog.records) == 1

    def test_tick_never_raises_even_when_the_journal_is_broken(self):
        c = _make_coord()
        c._promise_journal = None  # simulate a corrupted attribute
        c._tick_household_promises()  # must not raise


class TestMutexStillAlerts:
    def _wire_dedup(self, c):
        seen: dict[str, tuple] = {}

        def recent(key, sig, window_seconds=0):
            if seen.get(key) == sig:
                return True
            seen[key] = sig
            return False

        c.automation_engine._recent_duplicate = MagicMock(side_effect=recent)
        return seen

    def test_p2_notifies_and_emits_with_a_per_invariant_dedup_key(self):
        c = _make_coord(hvac_action="cooling", fan_on=True)
        seen = self._wire_dedup(c)
        with _now_patch():
            violations = c._run_invariant_watchdog(hvac_action="cooling")
            c._run_invariant_watchdog(hvac_action="cooling")
        assert [v.name for v in violations] == ["ac_whf_mutex"]
        assert "invariant_violation:ac_whf_mutex" in seen  # K4: its own slot, not a shared one
        c._notify_in_background.assert_called_once()
        assert c._emit_event.call_count == 1

    def test_promise_evaluation_does_not_double_report_the_mutex(self):
        c = _make_coord(hvac_action="cooling", fan_on=True)
        with _now_patch():
            c._record_promise_reading()
            reported = c._evaluate_household_promises()
        assert reported == []
        c._emit_event.assert_not_called()
        c._notify_in_background.assert_not_called()

    def test_p3_and_p2_dedupe_independently(self):
        c = _make_coord(hvac_action="cooling", fan_on=True, automation_enabled=False)
        self._wire_dedup(c)
        with _now_patch():
            c._promise_observe_command("fan", "turn_off", {"entity_id": "switch.whf"}, "fan")
            c._run_invariant_watchdog(hvac_action="cooling")
            c._evaluate_household_promises()
            c._run_invariant_watchdog(hvac_action="cooling")
            c._evaluate_household_promises()
        kinds = [call.args[1]["invariant"] for call in c._emit_event.call_args_list]
        assert sorted(kinds) == ["ac_whf_mutex", "observe_only_silence"]


class TestCommandObserver:
    def test_celsius_setpoint_is_journalled_in_fahrenheit(self):
        c = _make_coord(unit="celsius")
        with _now_patch():
            c._promise_observe_command(
                "climate", "set_temperature", {"temperature": 22.0, "hvac_mode": "heat"}, "climate"
            )
        (cmd,) = c._promise_journal.window(NOW).commands
        assert cmd.target == "thermostat" and cmd.mode == "heat"
        assert cmd.setpoint_f == 71.6 and cmd.indoor_f == 70.0 and cmd.origin == "ca"

    def test_fan_commands_both_directions_and_both_domains(self):
        c = _make_coord()
        with _now_patch():
            c._promise_observe_command("fan", "turn_on", {"entity_id": "fan.x"}, "fan")
            c._promise_observe_command("switch", "turn_off", {"entity_id": "switch.x"}, "fan")
        on, off = c._promise_journal.window(NOW).commands
        assert (on.target, on.fan_on) == ("fan", True) and (off.target, off.fan_on) == ("fan", False)

    def test_blind_indoor_is_stamped_none_so_promise_5_can_see_it(self):
        c = _make_coord()
        c._get_indoor_temp = lambda: None
        with _now_patch():
            c._promise_observe_command("fan", "turn_on", {"entity_id": "fan.x"}, "fan")
            reported = c._evaluate_household_promises()
        assert [h.promise for h in reported] == ["no_blind_fan_start"]

    def test_stamps_the_users_switch_not_the_engine_flag(self):
        c = _make_coord(automation_enabled=False)
        with _now_patch():
            c._promise_observe_command("climate", "set_hvac_mode", {"hvac_mode": "heat"}, "climate")
        assert c._promise_journal.window(NOW).commands[0].automation_enabled is False

    def test_observer_fault_is_swallowed(self):
        c = _make_coord()
        c._get_indoor_temp = MagicMock(side_effect=RuntimeError("boom"))
        with _now_patch():
            c._promise_observe_command("fan", "turn_on", {}, "fan")  # must not raise


def _make_engine(*, dry_run=False, shut_down=False):
    from custom_components.climate_advisor.automation import AutomationEngine

    eng = object.__new__(AutomationEngine)
    eng.dry_run = dry_run
    eng._shut_down = shut_down
    eng.role = "production"
    eng.hass = MagicMock()
    eng.hass.services.async_call = AsyncMock()
    eng._record_command_context = MagicMock()
    eng._command_observer = MagicMock()
    eng._observe_command = types.MethodType(AutomationEngine._observe_command, eng)
    eng.send = types.MethodType(AutomationEngine._call_service_with_context, eng)
    return eng


class TestEngineChokePointHook:
    def test_a_sent_command_is_observed_exactly_once(self):
        eng = _make_engine()
        asyncio.run(
            eng.send("climate", "set_hvac_mode", {"entity_id": "climate.t", "hvac_mode": "off"}, kind="climate")
        )
        eng._command_observer.assert_called_once_with(
            "climate", "set_hvac_mode", {"entity_id": "climate.t", "hvac_mode": "off"}, "climate"
        )
        eng.hass.services.async_call.assert_awaited_once()

    def test_observe_only_gate_sends_and_observes_nothing(self):
        eng = _make_engine(dry_run=True)
        asyncio.run(eng.send("climate", "set_hvac_mode", {"entity_id": "climate.t"}, kind="climate"))
        eng._command_observer.assert_not_called()
        eng.hass.services.async_call.assert_not_awaited()

    def test_shut_down_engine_sends_and_observes_nothing(self):
        eng = _make_engine(shut_down=True)
        asyncio.run(eng.send("fan", "turn_on", {"entity_id": "fan.x"}, kind="fan"))
        eng._command_observer.assert_not_called()
        eng.hass.services.async_call.assert_not_awaited()

    def test_a_journal_fault_never_blocks_the_command(self):
        eng = _make_engine()
        eng._command_observer = MagicMock(side_effect=RuntimeError("journal down"))
        asyncio.run(eng.send("fan", "turn_off", {"entity_id": "fan.x"}, kind="fan"))
        eng.hass.services.async_call.assert_awaited_once()

    def test_engine_without_an_observer_attribute_still_sends(self):
        """Older partial test stubs predate the attribute."""
        eng = _make_engine()
        del eng._command_observer
        asyncio.run(eng.send("fan", "turn_on", {"entity_id": "fan.x"}, kind="fan"))
        eng.hass.services.async_call.assert_awaited_once()


class TestPersistence:
    def _state(self, stats):
        cls = _coordinator_class()
        c = object.__new__(cls)
        c.hass = MagicMock()
        c._event_log = []
        c._current_classification = None
        c._today_record = None
        c._outdoor_temp_history = []
        c._indoor_temp_history = []
        c._briefing_sent_today = False
        c._last_briefing = ""
        c._last_briefing_short = ""
        c._briefing_day_type = None
        c._automation_enabled = True
        c._occupancy_mode = "home"
        c._occupancy_away_since = None
        c.claude_client = None
        c._pred_archive = {}
        for flag in (
            "_passive_k_backfilled",
            "_vent_window_k_backfilled",
            "_vent_fan_k_backfilled",
            "_passive_k_backfill_v2",
            "_vent_window_k_backfill_v2",
            "_vent_fan_k_backfill_v2",
            "_solar_phase_backfill",
            "_solar_phase_ac_backfill",
        ):
            setattr(c, flag, False)
        c._last_solar_phase_fit_date = None
        c.automation_engine = MagicMock()
        c.automation_engine.get_serializable_state = MagicMock(return_value={})
        c._promise_stats = stats
        return types.MethodType(cls._build_state_dict, c)()

    def test_promise_stats_are_persisted(self):
        stats = {"settling_flip": {"hits": 2, "first_ts": "a", "last_ts": "b"}}
        with _now_patch():
            assert self._state(stats)["promise_stats"] == stats

    def test_missing_attribute_persists_as_empty(self):
        cls = _coordinator_class()
        c = object.__new__(cls)
        # an old partial stub without _promise_stats must still serialise
        assert getattr(c, "_promise_stats", None) is None


class TestIssue1129FanReconcileEcho:
    """#1129: does a CA fan command from the reconcile path come back as a phantom manual override?

    Verdict (occupant-first): no. The reconcile path only runs in command-only mode, where the fan
    entity merely echoes CA's last command and the entity listener ignores every echo, so the
    occupant never gets a bogus 'you turned the fan on' grace period (their fan would otherwise
    be left alone for up to the grace length, or CA would stop managing it).
    """

    def _echo(self, c, old, new):
        return SimpleNamespace(
            data={"old_state": SimpleNamespace(state=old), "new_state": SimpleNamespace(state=new)},
            context=SimpleNamespace(id="not-ca", parent_id=None),
        )

    def test_command_then_echo_starts_no_override_for_both_directions(self):
        for on, old, new in ((True, "off", "on"), (False, "on", "off")):
            c = _make_coord(feedback=False)
            c._suppress_during_startup_coalescing = MagicMock(return_value=False)
            c.hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
            c.automation_engine.handle_fan_manual_override = AsyncMock()
            c.automation_engine.on_fan_turned_off = AsyncMock()
            c.automation_engine._fan_command_pending = False
            c.automation_engine._fan_override_active = False
            c.automation_engine._fan_active = False
            c.automation_engine.fan_command_context_matches = MagicMock(return_value=False)
            with _now_patch():
                assert asyncio.run(c._async_command_fan_entity(on=on)) is True
                asyncio.run(c._async_fan_entity_changed(self._echo(c, old, new)))
            c.hass.services.async_call.assert_awaited_once()
            c.automation_engine.handle_fan_manual_override.assert_not_awaited()
            c.automation_engine.on_fan_turned_off.assert_not_awaited()
            c.hass.async_create_task.assert_not_called()

    def test_the_command_is_journalled_even_though_it_bypasses_the_choke_point(self):
        c = _make_coord(feedback=False)
        with _now_patch():
            asyncio.run(c._async_command_fan_entity(on=True))
        (cmd,) = c._promise_journal.window(NOW).commands
        assert (cmd.target, cmd.fan_on, cmd.origin) == ("fan", True, "ca")

    def test_automation_off_means_no_command_and_nothing_journalled(self):
        c = _make_coord(automation_enabled=False, feedback=False)
        with _now_patch():
            assert asyncio.run(c._async_command_fan_entity(on=True)) is False
        c.hass.services.async_call.assert_not_awaited()
        assert c._promise_journal.window(NOW).commands == ()

    def test_every_caller_is_gated_on_command_only_mode(self):
        """The guarantee above holds only while no caller can run with fan-state feedback enabled."""
        src = Path(importlib.import_module(COORD_MOD).__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        callers = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_async_command_fan_entity"
        ]
        assert len(callers) == 2
        for call in callers:
            node, gated = call, False
            while node in parents:
                node = parents[node]
                if isinstance(node, ast.If) and "_fan_state_feedback_enabled" in ast.unparse(node.test):
                    gated = True
            assert gated, f"_async_command_fan_entity called at line {call.lineno} outside the command-only gate"


class TestRealEntryPointsFeedThePromises:
    """Drive the real entry points, not the observers: deleting a feed line must turn these red.

    Otherwise every promise except the mutex would silently stop evaluating at home with CI green.
    """

    @staticmethod
    def _callback_transparent_class():
        """A private copy of the coordinator class where ``@callback`` is the identity.

        In the lightweight HA stub ``callback`` is a MagicMock that swallows the decorated
        function (CLAUDE.md), so the real 5-minute tick callback is otherwise unreachable.
        Loaded under a private module name so the shared coordinator module is untouched.
        """
        import importlib.util

        real = importlib.import_module(COORD_MOD)
        core = sys.modules["homeassistant.core"]
        saved = core.callback
        core.callback = lambda fn: fn
        try:
            name = f"{COORD_MOD}_callback_probe"
            spec = importlib.util.spec_from_file_location(name, real.__file__)
            probe = importlib.util.module_from_spec(spec)
            probe.__package__ = "custom_components.climate_advisor"
            sys.modules[name] = probe
            try:
                spec.loader.exec_module(probe)
            finally:
                sys.modules.pop(name, None)
        finally:
            core.callback = saved
        return probe.ClimateAdvisorCoordinator

    def _entry_coord(self):
        c = _make_coord(hvac_action="heating")
        probe_cls = self._callback_transparent_class()
        for name in ("_async_thermal_sample_tick", "_async_thermostat_changed", "_emit_event"):
            setattr(c, name, types.MethodType(getattr(probe_cls, name), c))
        return c

    def test_the_real_five_minute_tick_records_and_evaluates(self):
        c = self._entry_coord()
        c._refresh_outdoor_temp = MagicMock()
        c._refresh_indoor_sensor_health = MagicMock()
        c._sample_all_observations = MagicMock()
        with _now_patch():
            c._async_thermal_sample_tick(NOW)
        window = c._promise_journal.window(NOW)
        assert window.latest is not None and window.latest.hvac_action == "heating"
        assert c._promise_armed_logged is True  # the evaluator ran, not just the recorder

    def test_the_real_thermostat_listener_records_a_reading_even_on_its_earliest_exit(self):
        c = self._entry_coord()
        event = SimpleNamespace(data={"new_state": None, "old_state": None})
        with _now_patch():
            asyncio.run(c._async_thermostat_changed(event))
        assert c._promise_journal.window(NOW).latest is not None

    def test_the_real_emit_event_feeds_lifecycle_marks_to_the_journal(self):
        c = self._entry_coord()
        c._emit_event = types.MethodType(_coordinator_class()._emit_event, c)
        c._event_log = []
        c._nat_vent_was_active = False
        c._feed_lifecycle_fsms_from_event = MagicMock()
        with _now_patch():
            c._emit_event("override_detected", {"detected_mode": "heat", "source": "manual"})
        (episode,) = c._promise_journal.window(NOW).episodes
        assert (episode.target, episode.kind, episode.value) == ("thermostat", "override", "heat")

    def test_an_unmapped_event_adds_no_mark(self):
        c = self._entry_coord()
        c._emit_event = types.MethodType(_coordinator_class()._emit_event, c)
        c._event_log = []
        c._nat_vent_was_active = False
        c._feed_lifecycle_fsms_from_event = MagicMock()
        with _now_patch():
            c._emit_event("briefing_sent", {})
        assert c._promise_journal.window(NOW).marks == ()
