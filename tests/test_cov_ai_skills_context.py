"""Coverage for the AI Investigator / Activity Report context builders and event renderers (ai_skills_context.py).

Occupant view: the AI report and the Activity Record are only as good as what they are told. Each event row must
say in plain words what happened at home (a window opened and the AC was turned off, a fan switch ignored a command,
the house sat above the comfort range for hours), and one broken data source (a learning call that raises, an
unreachable GitHub, a corrupt record) must be reported as "unavailable" in its own section instead of losing the
whole report.
"""

from __future__ import annotations

import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import custom_components.climate_advisor.ai_skills_context as m

_UNIT = "fahrenheit"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _ago(minutes: float) -> str:
    return (_now() - datetime.timedelta(minutes=minutes)).isoformat()


def _run(coro):
    return asyncio.run(coro)


class _Raises:
    """A coordinator stand-in whose ``name`` attribute raises (a data source that is broken, not missing)."""

    def __init__(self, *names: str, **attrs):
        self._names = names
        self.__dict__.update(attrs)

    def __getattr__(self, item):
        if item in object.__getattribute__(self, "_names"):
            raise RuntimeError(f"{item} broken")
        raise AttributeError(item)


# ---------------------------------------------------------------------------
# Event renderers (what each Activity row says)
# ---------------------------------------------------------------------------

_RENDER_CASES = [
    (
        "tou_schedule_window_active",
        {"skipped": [{"reason": "manual", "entity": "climate.x"}, "junk"]},
        ("TOU high-cost period skipped", "its condition is off"),
    ),
    (
        "occupancy_setback",
        {"occupancy": "away", "floor": 60, "ceiling": 80, "tou_bank": True, "target_f": 78, "hvac_mode": "cool"},
        (
            "Occupancy setback (away)",
            "sent cool 78°F (time-of-use bank owns the setpoint; band setpoint: 60°F Heat / 80°F Cool after it)",
        ),
    ),
    (
        "occupancy_setback",
        {"occupancy": "away", "floor": 60, "ceiling": 80, "tou_bank": True, "target_f": "bad"},
        ("Occupancy setback (away)", "setpoint: 60°F Heat / 80°F Cool"),
    ),
    (
        "occupancy_comfort_restored",
        {"mode": "heat", "target_f": 70, "tou_bank": True},
        (
            "Occupancy -- comfort restored (heat)",
            "setpoint: 70°F (time-of-use bank owns the setpoint; comfort band after it)",
        ),
    ),
    ("occupancy_comfort_restored", {"mode": "heat", "target_f": "bad"}, ("Occupancy -- comfort restored (heat)", "")),
    ("pre_cool_applied", {"target": 72, "floor": 68}, ("Pre-cool applied", "setpoint: 72°F Cool (68°F Heat)")),
    ("pre_cool_applied", {"target": 72}, ("Pre-cool applied", "setpoint: 72°F Cool")),
    ("pre_cool_applied", {"target": "bad"}, ("Pre-cool applied", "")),
    ("ceiling_guard_fired", {"old_setpoint_f": "x", "new_setpoint_f": 70}, ("ODE ceiling guard fired", "")),
    (
        "hvac_mode_not_applied",
        {"kind": "open_window_retry", "requested_mode": "off", "actual_mode": "cool"},
        ("Thermostat still cool with a window open -- re-sending off", "requested off, thermostat reports cool"),
    ),
    (
        "hvac_mode_not_applied",
        {"kind": "restore_retry", "requested_mode": "cool"},
        ("Thermostat still ? after Climate Advisor restored cool -- re-sending", "requested cool"),
    ),
    (
        "hvac_mode_not_applied",
        {"kind": "other", "requested_mode": "heat"},
        ("Thermostat did not switch to heat -- it may be ignoring mode commands", "requested heat"),
    ),
    (
        "hvac_output_stall",
        {"direction": "cool", "hours": 3, "edge_f": 75, "thermostat_mode": "cool", "thermostat_action": "idle"},
        (
            "House is above the comfort band and the cooling hasn't run for 3 hours",
            "band edge 75°F, thermostat mode cool, action idle",
        ),
    ),
    ("override_cleared", {"was_mode": "heat", "old_setpoint_f": "bad"}, ("Override cleared (was heat)", "")),
    ("override_cleared", {"reason": "fan off"}, ("Override cleared", "fan override — fan off")),
    ("override_self_resolved", {}, ("Override self-resolved (transient)", "")),
    (
        "entity_unavailable",
        {"entity_id": "sensor.a", "status": "missing", "down_since": "2026-06-17T10:00:00+00:00", "detail": "gone"},
        ("sensor.a not found", "since 10:00, gone"),
    ),
    (
        "entity_recovered",
        {"friendly_name": "Thermostat", "outage_minutes": 12, "down_since": "2026-06-17T10:00:00+00:00"},
        ("Thermostat available again", "out 12 min, since 10:00"),
    ),
    (
        "indoor_source_changed",
        {"to_name": "Bedroom", "from_name": "Hall", "reason": "primary lost", "to_value_f": 70.5},
        ("Room temperature now from Bedroom instead of Hall", "primary lost, now 70.5°F"),
    ),
    (
        "humidity_source_changed",
        {"subject": "room humidity", "from_name": "A", "reason": "lost", "to_rh": 40},
        ("Room humidity: no humidity source (was A)", "lost, now 40%"),
    ),
    ("forecast_model_changed", {"detail_key": "activity.forecast_model_changed"}, ("Prediction model changed",) * 2),
    (
        "forecast_model_changed",
        {"detail_template": "diff {t0}", "temps_f": {"t0": 1.5}},
        ("Prediction model changed", "diff 1.50°F"),
    ),
    (
        "forecast_model_changed",
        {"detail_template": "diff {t9}", "temps_f": {"t0": 1.5}},
        ("Prediction model changed", ""),
    ),
    ("forecast_model_changed", {"detail_template": 5}, ("Prediction model changed", "")),
    (
        "whf_rule2_backstop",
        {"stopped": "fan"},
        ("Whole-house fan stopped -- heating/cooling was running beside it (newest action wins)", ""),
    ),
    ("fan_manual_override", {"restored_from": "x"}, ("Fan manual override", "restored after restart")),
    (
        "fan_orphan_released",
        {"old_fan_mode": "on", "command_sent": "auto"},
        (
            "Fan released after configuration change",
            "thermostat fan_mode on -> auto (CA had left it on under the previous configuration)",
        ),
    ),
    (
        "fan_cancel",
        {"trigger": "physical_drift_correction", "readback": "off", "retry": True, "session_kept": False},
        ("Fan switch ignored the on-command -- ventilation ended, AC released", "fan: ?->?"),
    ),
    (
        "fan_cancel",
        {"trigger": "physical_drift_correction", "readback": "off", "retry": True},
        ("Fan switch ignored the on-command -- retrying once", "fan: ?->?"),
    ),
    (
        "fan_cancel",
        {"trigger": "physical_drift_correction", "readback": "off", "session_kept": False},
        ("Fan switch ignored the on-command again -- giving up on the fan, AC released", "fan: ?->?"),
    ),
    (
        "fan_cancel",
        {"trigger": "physical_drift_correction", "readback": "off"},
        (
            "Fan switch ignored the on-command again -- giving up on the fan (AC stays off while windows are open)",
            "fan: ?->?",
        ),
    ),
    (
        "nat_vent_comfort_floor_exit",
        {"indoor_temp": 66, "comfort_heat": 68, "hvac_restore": {"mode": "heat"}},
        ("Nat-vent exit -- indoor 66°F <= floor 68°F (fan: on->off)", "mode: off->heat"),
    ),
    (
        "nat_vent_manual_override_exit",
        {"override_mode": "cool", "indoor_temp": "bad"},
        ("Nat-vent exit -- manual override to cool (fan: on->off)", ""),
    ),
    (
        "nat_vent_reactivated_while_paused",
        {"outdoor": 60, "indoor": 72, "threshold": 3},
        ("Nat-vent reactivated while paused -- outdoor 60°F cooled below indoor 72°F", "threshold: 3°F"),
    ),
    (
        "sensor_opened",
        {"entity": "binary_sensor.door", "hvac_mode_set": {"mode": "off"}, "hvac_mode_before": "cool"},
        ("Sensor opened: binary_sensor.door", "mode: cool->off"),
    ),
    (
        "sensor_opened",
        {"entity": "binary_sensor.door", "hvac_mode_set": "notadict"},
        ("Sensor opened: binary_sensor.door", ""),
    ),
    (
        "sensor_opened",
        {"entity": "binary_sensor.door", "threshold": 3},
        ("Sensor opened: binary_sensor.door", "threshold 3°F"),
    ),
    (
        "nat_vent_forecast_skip",
        {"forecast_peak": 85, "threshold": 80},
        ("Nat-vent skipped -- forecast peak 85°F > threshold 80°F", ""),
    ),
    (
        "bedtime_setback_skipped",
        {"reason": "occupancy", "occupancy": "away"},
        ("Bedtime setback skipped -- away mode active", ""),
    ),
    (
        "pre_cool_overshoot",
        {"indoor": 67, "comfort_heat": 68},
        ("Pre-cool overshoot -- indoor 67°F < floor 68°F", ""),
    ),
    (
        "startup_coalesced",
        {"hvac_commanded": True, "fan_archetype": "whf"},
        ("Startup coalescing complete -- HVAC commanded", "fan: whf"),
    ),
    (
        "stuck_grace_recovered",
        {"grace_end_time": "2026-06-17T10:00:00+00:00"},
        ("Grace period recovered (expired 2026-06-17T10:00:00+00:00)", ""),
    ),
    (
        "invariant_violation",
        {"invariant": "AC_AND_WHF", "promise": "p1", "severity": "log"},
        ("Noted, not alerted: Hard invariant violated: AC_AND_WHF", "promise p1: AC_AND_WHF (log)"),
    ),
    (
        "invariant_violation",
        {"invariant": "AC_AND_WHF", "promise": "p1"},
        ("Hard invariant violated: AC_AND_WHF", "promise p1: AC_AND_WHF"),
    ),
    ("warm_day_setback_applied", {"old_setpoint_f": "x", "new_setpoint_f": 70}, ("Warm-day setback applied", "")),
    (
        "thermostat_mode_set",
        {"to_mode": "heat", "indoor_f": 68, "outcome": "failed"},
        ("Thermostat set to heat", "requested — indoor 68°F — not applied (failed)"),
    ),
    (
        "comfort_switch_held",
        {"candidate_family": "heat", "zone": "x", "indoor_f": 70},
        ("Switch to heat held (opposite side ran recently)", "indoor 70°F"),
    ),
    (
        "classification_suppressed_paused",
        {"paused_entity": "binary_sensor.door"},
        ("Classification suppressed — windows open", "binary_sensor.door"),
    ),
]


@pytest.mark.parametrize(("event_type", "payload", "expected"), _RENDER_CASES)
def test_event_row_text(event_type, payload, expected):
    assert m.EVENT_RENDERERS[event_type](payload, _UNIT) == expected


class TestRendererEdgeCases:
    """Rows whose wording depends on optional fields; checked for what the occupant reads, not exact text."""

    def test_override_self_resolved_names_both_modes(self):
        label, _ = m.EVENT_RENDERERS["override_self_resolved"]({"detected_mode": "heat", "current_mode": "cool"}, _UNIT)
        assert "heat" in label and "cool" in label

    def test_humidity_source_changed_to_a_new_sensor(self):
        label, settings = m.EVENT_RENDERERS["humidity_source_changed"](
            {"to_entity": "sensor.rh", "to_name": "Bedroom RH", "to_kind": "room", "from_name": "Hall", "from_rh": 55},
            _UNIT,
        )
        assert "Bedroom RH" in label and "Hall" in label
        assert "55%" in settings

    def test_indoor_source_reason_as_message(self):
        _, settings = m.EVENT_RENDERERS["indoor_source_changed"](
            {
                "to_name": "A",
                "from_name": "B",
                "reason_key": "activity.join_comma_2",
                "reason_params": {"a": "x", "b": "y"},
            },
            _UNIT,
        )
        assert settings == "x, y"

    def test_malformed_stored_message_falls_back_to_english(self):
        cell = m._field({"reason_key": "x", "reason_params": "not-a-dict", "reason": "fallback"}, "reason")
        assert cell == "fallback"

    def test_none_field_uses_default(self):
        assert m._field({"reason": None}, "reason", default="dflt") == "dflt"

    def test_long_join_nests(self):
        parts = [str(i) for i in range(8)]
        assert m._cell_en(m._join(parts)) == ", ".join(parts)

    def test_whf_rule2_hvac_off_with_mode(self):
        label, settings = m.EVENT_RENDERERS["whf_rule2_backstop"]({"thermostat_mode": "cool"}, _UNIT)
        assert "cool" in settings and label

    def test_fan_manual_override_remote(self):
        _, settings = m.EVENT_RENDERERS["fan_manual_override"](
            {"fan_before": "off", "fan_after": "on", "remote_speed": "high", "remote_timer_hours": 4}, _UNIT
        )
        assert "off->on" in settings and "high" in settings and "4" in settings

    def test_fan_orphan_dry_run_left_as_is(self):
        label, settings = m.EVENT_RENDERERS["fan_orphan_released"]({"dry_run": True, "old_fan_mode": "on"}, _UNIT)
        assert label != "Fan released after configuration change" and "on" in settings

    def test_floor_exit_from_before_issue_1268(self):
        _, settings = m.EVENT_RENDERERS["nat_vent_comfort_floor_exit"]({"hvac_mode_restored": "heat"}, _UNIT)
        assert settings == "mode: off->heat"

    def test_manual_override_exit_with_indoor(self):
        label, _ = m.EVENT_RENDERERS["nat_vent_manual_override_exit"](
            {"override_mode": "heat", "indoor_temp": 65}, _UNIT
        )
        assert "heat" in label and "65°F" in label

    def test_bedtime_skipped_for_another_reason(self):
        label, _ = m.EVENT_RENDERERS["bedtime_setback_skipped"]({"reason": "windows open"}, _UNIT)
        assert "windows open" in label

    def test_stuck_grace_unparseable_end_kept_as_is(self):
        with patch.object(m.dt_util, "parse_datetime", side_effect=ValueError("bad")):
            label, _ = m.EVENT_RENDERERS["stuck_grace_recovered"]({"grace_end_time": "soon"}, _UNIT)
        assert label == "Grace period recovered (expired soon)"

    def test_local_time_unreadable_is_blank(self):
        with patch.object(m.dt_util, "parse_datetime", side_effect=TypeError("bad")):
            assert m._local_hhmm("2026-06-17T10:00:00+00:00") == ""

    def test_incident_with_band_and_last_command(self):
        _, settings = m.EVENT_RENDERERS["incident_detected"](
            {
                "incident_class": "comfort",
                "indoor_f": 80,
                "comfort_heat": 68,
                "comfort_cool": 75,
                "last_commanded_mode": "cool",
                "last_commanded_setpoint_f": "bad",
            },
            _UNIT,
        )
        assert "80°F" in settings and "cool" in settings

    def test_paused_entity_with_minutes(self):
        assert "12" in m._cell_en(m._render_paused_entity_settings({"paused_entity": "door", "paused_minutes": 12.7}))

    def test_comfort_switch_held_in_margin(self):
        _, settings = m.EVENT_RENDERERS["comfort_switch_held"]({"zone": "margin", "tolerance_f": 0.5}, _UNIT)
        assert "0.5" in settings

    def test_warm_day_setback_with_setpoints(self):
        _, settings = m.EVENT_RENDERERS["warm_day_setback_applied"]({"old_setpoint_f": 72, "new_setpoint_f": 76}, _UNIT)
        assert "72" in settings and "76" in settings

    def test_ceiling_guard_mode_change_only(self):
        _, settings = m.EVENT_RENDERERS["ceiling_guard_fired"]({"old_hvac_mode": "off", "new_hvac_mode": "cool"}, _UNIT)
        assert settings == "mode: off->cool"

    def test_override_cleared_with_setpoint(self):
        _, settings = m.EVENT_RENDERERS["override_cleared"]({"old_setpoint_f": 71}, _UNIT)
        assert "71°F" in settings

    def test_unregistered_event_type_uses_generic_facts(self):
        label, settings = m._default_renderer(
            "unknown_type_xyz",
            {
                "old_mode": "heat",
                "new_mode": "cool",
                "old_setpoint_f": 70,
                "new_setpoint_f": "bad",
                "floor": 60,
                "ceiling": 80,
                "fan_mode_change": "on",
            },
            _UNIT,
        )
        assert label == "Unknown type xyz"
        assert settings == "mode: heat->cool, setpoint: 60°F Heat / 80°F Cool, fan: on"


class TestModeWriteFact:
    def test_not_a_record(self):
        assert m._mode_write_fact("x") == ""

    def test_held_says_why(self):
        assert m._cell_en(m._mode_write_fact({"held_reason": "window_open"})) != ""

    def test_no_mode_is_blank(self):
        assert m._mode_write_fact({"outcome": "sent"}) == ""

    def test_change_from_previous_mode(self):
        assert m._cell_en(m._mode_write_fact({"mode": "off"}, before="cool")) == "mode: cool->off"

    def test_not_sent_says_so(self):
        text = m._cell_en(m._mode_write_fact({"mode": "heat", "outcome": "failed"}))
        assert text.startswith("mode: off->heat") and text != "mode: off->heat"


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------


def test_window_compliance_with_denominator():
    assert m._fmt_window_compliance({"window_compliance": 0.5, "window_compliance_denominator": 4}) == (
        "0.5000 (2 of 4 windows-recommended days)"
    )


def test_engine_line_without_details():
    text = m.format_engine_status_for_ai({"k_passive": {"active": True, "value": 0.1}})
    assert "  k_passive: (0.1000 hr^-1) [ACTIVE]" in text.splitlines()


@pytest.mark.parametrize(("hours", "text"), [(12, "12h"), (48, "2d"), (36, "1.5d")])
def test_fmt_hours(hours, text):
    assert m._fmt_hours(hours) == text


def test_grace_without_a_source_is_unknown():
    assert m._event_source_label("grace_started", {}) is None


def test_filter_events_accepts_a_naive_now():
    naive_now = _now().replace(tzinfo=None)
    kept, limited = m.filter_events_by_window([{"time": _ago(10)}, {"time": _ago(600)}], 1, naive_now)
    assert len(kept) == 1 and limited is False


def test_command_only_whole_house_fan_gets_a_warning_banner():
    cfg = {"fan_mode": "whole_house_fan", "fan_entity": "switch.whf", "fan_state_feedback": False}
    assert m._whf_warning_applies(cfg)
    assert m._maybe_prepend_whf_warning("TABLE", cfg).endswith("\n\nTABLE")
    assert m._maybe_prepend_whf_warning("TABLE", {}) == "TABLE"


def test_a_renderer_that_raises_still_gives_a_row(monkeypatch):
    def _boom(p, unit):
        raise RuntimeError("renderer bug")

    monkeypatch.setitem(m.EVENT_RENDERERS, "pre_cool_applied", _boom)
    table = m.build_event_timeline_table([{"type": "pre_cool_applied", "time": _ago(5)}], {}, 24, _now())
    assert "| Pre cool applied |  |" in table


# ---------------------------------------------------------------------------
# Daily summaries
# ---------------------------------------------------------------------------


def test_daily_summaries_render_past_days_only():
    today = datetime.date.today()
    records = [
        {"date": (today - datetime.timedelta(days=1)).isoformat(), "day_type": "warm", "avg_indoor_temp": 71.2},
        {"date": today.isoformat(), "day_type": "cold"},
    ]
    coord = SimpleNamespace(config={}, learning=SimpleNamespace(_state=SimpleNamespace(records=records)))
    lines = m._build_daily_summaries(coord, 72)
    assert lines[1].startswith("## HISTORICAL DAILY SUMMARIES (last 3 days")
    assert any("warm" in ln and "71.2°F" in ln for ln in lines)
    assert not any("cold" in ln for ln in lines)


def test_daily_summaries_failure_is_skipped():
    assert m._build_daily_summaries(SimpleNamespace(config={}), "abc") == []


# ---------------------------------------------------------------------------
# Section builders: failures stay inside their own section
# ---------------------------------------------------------------------------


def test_current_state_unavailable():
    coord = MagicMock(data={})
    coord.get_hvac_runtime_today.side_effect = RuntimeError("no runtime")
    assert _run(m.build_current_state_context(None, coord)) == "=== CURRENT STATE ===\n  unavailable\n"


def test_last_briefing_unavailable():
    assert _run(m.build_last_briefing_context(None, _Raises("_last_briefing"))) == (
        "=== LAST BRIEFING ===\n  unavailable\n"
    )


class TestHvacEntity:
    def test_remote_sensor_offset_lines(self):
        coord = MagicMock(data={"hvac_mode": "heat"}, config={"climate_entity": ""})
        with (
            patch.object(m, "remote_offset_view_for", return_value={"state": "active"}),
            patch.object(m, "remote_offset_lines", return_value=["    state: active"]),
        ):
            text = _run(m.build_hvac_entity_context(MagicMock(), coord))
        assert "  remote_sensor_offset:\n    state: active" in text
        assert "entity_id:        not configured" in text

    def test_unavailable(self):
        assert _run(m.build_hvac_entity_context(None, _Raises("data"))) == "=== HVAC ENTITY ===\n  unavailable\n"


def test_config_unavailable():
    assert _run(m.build_config_context(None, SimpleNamespace(config=5))) == "=== CONFIGURATION ===\n  unavailable\n"


def test_swing_lines_explain_measured_swing_rejections_and_coast():
    thermal = {
        "swing_definition": "half the dead band",
        "swing_heat_f": 1.0,
        "swing_heat_raw_f": 0.8,
        "swing_heat_resolution_f": 0.5,
        "observation_count_swing_heat": 7,
        "coast_heat_f": 0.4,
        "observation_count_coast_heat": 3,
        "swing_debug": {
            "heat": {
                "reject_counts": {"short": 2, "gap": 5},
                "recent": [{"accepted": True, "value_f": 1.0}, {"accepted": False, "reason": "short"}],
            }
        },
    }
    lines = m._swing_context_lines(thermal, {"temp_unit": _UNIT})
    text = "\n".join(lines)
    assert "heat swing: 1.0°F below setpoint (median 0.80°F rounded up" in text
    assert "7 accepted in total, readings quantised to 0.5°F)" in text
    assert "rejected cycles (not hysteresis): gap x5, short x2" in text
    assert "recent cycles: accepted 1.0°F; rejected short" in text
    assert "heat coast (kept apart from the swing): the room goes 0.4°F further" in text
    assert "cool swing: not measured yet (0 accepted cycles" in text


class TestLearningContext:
    def test_every_part_failing_is_reported_per_section(self):
        learning = MagicMock()
        learning.get_compliance_summary.side_effect = RuntimeError("x")
        learning.get_thermal_model.side_effect = RuntimeError("x")
        learning.get_weather_bias.side_effect = RuntimeError("x")
        learning.generate_suggestions.side_effect = RuntimeError("x")
        learning._state = MagicMock(records=["bad", {"date": "d"}])
        type(learning._state).records = property(lambda self: (_ for _ in ()).throw(RuntimeError("x")))
        coord = MagicMock(learning=learning, config={})
        text = _run(m.build_learning_context(None, coord, hours=48))
        for header in ("COMPLIANCE SUMMARY", "THERMAL MODEL", "WEATHER BIAS", "ACTIVE SUGGESTIONS", "DAILY RECORDS"):
            assert f"{header} ===\n  unavailable" in text

    def test_suggestions_and_records_are_listed(self):
        learning = MagicMock()
        learning.get_compliance_summary.return_value = {}
        learning.get_thermal_model.return_value = {}
        learning.get_weather_bias.return_value = {}
        learning.generate_suggestions.return_value = [
            {"suggestion_type": "low_window_compliance", "text": "Open windows", "evidence": {"days": 3}},
            "ignored",
        ]
        learning._state = MagicMock(
            records=[
                "ignored",
                {"date": "2026-11-20", "windows_recommended": True, "windows_physically_opened": True},
            ]
        )
        text = _run(m.build_learning_context(None, MagicMock(learning=learning, config={}), hours=24))
        assert "  [1] type=low_window_compliance\n      text: Open windows\n      evidence: {'days': 3}" in text
        assert "  2026-11-20: opened=True window_rec=opened" in text

    def test_no_learning_engine(self):
        assert _run(m.build_learning_context(None, SimpleNamespace(learning=None))) == (
            "=== LEARNING ===\n  learning engine not available\n"
        )

    def test_unreadable_learning_engine(self):
        assert _run(m.build_learning_context(None, _Raises("learning"))) == "=== LEARNING ===\n  unavailable\n"


class TestThermalPipeline:
    def _coord(self, rows=None, **overrides):
        coord = MagicMock(config={"temp_unit": _UNIT})
        coord._build_learning_health.return_value = {}
        coord.learning.get_thermal_model.return_value = {}
        coord.learning.get_engine_status.return_value = {}
        coord.prediction_refit_note.return_value = ""
        coord.decision_rates_summary.return_value = rows or []
        for k, v in overrides.items():
            setattr(coord, k, v)
        return coord

    def test_cycle_model_rows_and_rates_in_use(self):
        rows = [
            {"source": "notice", "text": "whole-degree sensor", "rate_in_use": "default"},
            {"source": "cycle", "key": "k_active_cool", "value": {"q": "n/a"}},
            {
                "source": "cycle",
                "mode": "heat",
                "value": {
                    "q": 2.5,
                    "sd_q": 0.3,
                    "grade": "medium",
                    "coast_area_f": 0.5,
                    "ne": 12,
                    "lag_min": 4.0,
                    "lag_identified": False,
                    "grade_caps": ["guessed_leak"],
                    "coarse_sensor": {"text": "half-degree readings", "rate_in_use": "stored"},
                },
            },
            {"key": "k_passive", "value": 0.05, "source": "v4"},
            {"key": "k_vent_fan", "value": None, "source": "v3"},
        ]
        text = _run(m.build_thermal_pipeline_context(None, self._coord(rows)))
        assert "  thermostat: whole-degree sensor (rate in use: default)" in text
        assert (
            "  cycle model: heat rate 2.50 +/- 0.30 °F/h, grade medium, coast after a run +0.50 °F,"
            " 12 informative cycles, sensor lag 4.0 min (not identified), grade held down by guessed_leak"
        ) in text
        assert "  cycle model heat sensor: half-degree readings (rate in use: stored)" in text
        assert "cool rate 0" not in text and "rate n/a" not in text
        assert "Rates in use (decisions and timeline):\n  k_passive: 0.0500 hr^-1 (source v4)" in text
        assert "  k_vent_fan: not learned (source v3)" in text

    def test_every_optional_source_failing_still_builds(self):
        coord = self._coord()
        coord._build_learning_health.side_effect = RuntimeError("x")
        coord.learning.get_thermal_model.side_effect = RuntimeError("x")
        coord.learning.get_engine_status.side_effect = RuntimeError("x")
        coord.prediction_refit_note.side_effect = RuntimeError("x")
        coord.decision_rates_summary.side_effect = RuntimeError("x")
        text = _run(m.build_thermal_pipeline_context(None, coord))
        assert text.startswith("=== THERMAL OBSERVATION PIPELINE ===")
        assert "Engine status:\n  unavailable" in text
        assert "NEVER LEARNED — k_active_heat is None" in text

    def test_refit_note_is_shown(self):
        coord = self._coord()
        coord.prediction_refit_note.return_value = "refits unusable; learned model kept"
        assert "Prediction model: refits unusable; learned model kept" in _run(
            m.build_thermal_pipeline_context(None, coord)
        )


class TestEventLogContext:
    def test_captured_records_are_zone_scoped(self):
        handler = MagicMock()
        handler.get_records.return_value = [
            {"time": "garbage", "zone": "z1", "level": "WARNING", "logger_name": "ca", "message": "undated"},
            {"time": _ago(5), "zone": "z1", "level": "ERROR", "logger_name": "ca", "message": "mine"},
            {"time": _ago(5), "zone": None, "level": "WARNING", "logger_name": "ca", "message": "untagged"},
            {"time": _ago(5), "zone": "z2", "level": "ERROR", "logger_name": "ca", "message": "other zone"},
            {"time": _ago(60 * 24 * 40), "zone": "z1", "level": "ERROR", "logger_name": "ca", "message": "old"},
        ]
        coord = SimpleNamespace(_event_log=[], zone_label="z1")
        with (
            patch.object(m.log_capture, "get_handler", return_value=handler),
            patch.object(m.zone_registry, "resolve_zone_display_name", side_effect=lambda h, z: f"Zone {z}"),
        ):
            text = _run(m.build_event_log_context(MagicMock(), coord, hours=24))
        assert "=== SYSTEM LOG RECORDS (WARNING+, last 24h, 3 records, zone=Zone z1) ===" in text
        assert "(zone=Zone z1) undated" in text and "(zone=Zone z1) mine" in text
        assert "(zone=unknown zone) untagged" in text
        assert "other zone" not in text and " old" not in text

    def test_every_section_failing_is_reported(self):
        coord = SimpleNamespace(_event_log=[{"type": "x", "time": _ago(1)}])
        with (
            patch.object(m, "filter_events_by_window", side_effect=RuntimeError("x")),
            patch.object(m.log_capture, "get_handler", side_effect=RuntimeError("x")),
            patch.object(m, "_build_restart_summary", side_effect=RuntimeError("x")),
        ):
            text = _run(m.build_event_log_context(MagicMock(), coord, hours=24))
        assert "=== EVENT LOG ===\n  unavailable" in text
        assert "=== SYSTEM LOG RECORDS ===\n  unavailable" in text
        assert "=== TIMING CORRELATIONS ===\n  unavailable" in text
        assert "=== KNOWN OVERRIDE FALSE POSITIVES (Issue #205) ===\n  unavailable" in text
        assert "=== RESTART HISTORY ===\n  unavailable" in text


def test_override_false_positive_accepts_naive_and_unparseable_times():
    base = _now().replace(tzinfo=None, microsecond=0)
    events = [
        {"type": "nat_vent_fan_on", "time": base - datetime.timedelta(seconds=30)},
        {"type": "override_detected", "time": base.isoformat()},
        {"type": "override_detected", "time": "not-a-time"},
    ]
    text = m._build_known_override_false_positives(events, 24, _now())
    assert "is 30s from nat_vent_fan_on" in text and "known false override detection" in text


def test_timing_correlations_accept_naive_and_unparseable_times():
    base = _now().replace(tzinfo=None, microsecond=0)
    events = [
        {"type": "classification_applied", "source": "automation", "time": base - datetime.timedelta(minutes=30)},
        {"type": "override_detected", "source": "manual", "time": base.isoformat()},
        {"type": "override_detected", "source": "manual", "time": "not-a-time"},
    ]
    text = m._build_timing_correlations(events, 24, _now())
    assert text.startswith("=== TIMING CORRELATIONS ===")
    assert text.count("override_detected") == 1


# ---------------------------------------------------------------------------
# GitHub issues
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


def _session(*responses):
    session = MagicMock()
    session.get.side_effect = list(responses)
    return session


class TestGithubIssues:
    def test_fresh_caches_need_no_request(self):
        coord = SimpleNamespace(
            _github_open_cache=[{"number": 1, "title": "Open bug", "state": "open", "labels": [{"name": "high"}]}],
            _github_open_cache_ts=10**12,
            _github_closed_cache=[{"number": 2, "title": "Closed bug", "state": "closed", "labels": []}],
            _github_closed_cache_ts=10**12,
        )
        session = _session()
        with patch.object(m, "_client_session", return_value=session), patch("time.monotonic", return_value=10**12):
            text = _run(m._fetch_github_issues(MagicMock(), coord))
        session.get.assert_not_called()
        assert "- #1 (open) [high]: Open bug" in text and "- #2 (closed): Closed bug" in text

    def test_github_errors_give_no_section(self):
        with patch.object(m, "_client_session", return_value=_session(_Resp(503, None), _Resp(403, None))):
            assert _run(m._fetch_github_issues(MagicMock(), None)) == ""

    def test_no_coordinator_fetches_and_does_not_cache(self):
        issue = {"number": 3, "title": "T", "state": "open", "labels": [], "body": "dropped"}
        with patch.object(m, "_client_session", return_value=_session(_Resp(200, [issue]), _Resp(200, []))):
            assert "- #3 (open): T" in _run(m._fetch_github_issues(MagicMock(), None))

    def test_network_error_uses_stale_cache(self):
        coord = SimpleNamespace(
            _github_open_cache=[{"number": 4, "title": "Stale", "state": "open", "labels": []}],
            _github_open_cache_ts=0.0,
            _github_closed_cache=None,
            _github_closed_cache_ts=0.0,
        )
        with patch.object(m, "_client_session", side_effect=RuntimeError("offline")):
            assert "- #4 (open): Stale" in _run(m._fetch_github_issues(MagicMock(), coord))


# ---------------------------------------------------------------------------
# Manual overrides and fan ownership
# ---------------------------------------------------------------------------


def _engine(**attrs):
    return SimpleNamespace(
        _manual_override_active=False, _manual_override_time=None, _grace_end_time=None, _grace_active=False, **attrs
    )


class TestOverrideDetails:
    def test_today_overrides_are_listed_in_the_users_unit(self):
        today = SimpleNamespace(
            manual_overrides=2,
            override_details=[
                {"time": "08:00", "old_temp": 68, "new_temp": 72, "direction": "up", "magnitude": 4},
                {"old_temp": None},
            ],
        )
        coord = SimpleNamespace(_today_record=today, config={"temp_unit": _UNIT}, automation_engine=None)
        text = _run(m.build_override_details_context(None, coord))
        assert "  Setpoint override count: 2" in text
        assert "  #1  08:00  68.0°F -> 72.0°F  (+4.0°F, up)" in text
        assert "  #2  ??:??  ? -> ?  (-?, ?)" in text

    def test_active_override_duration(self):
        start = (_now() - datetime.timedelta(minutes=30)).isoformat()
        engine = _engine()
        engine._manual_override_active = True
        engine._manual_override_time = start
        engine._grace_active = True
        coord = SimpleNamespace(_today_record=None, config={}, automation_engine=engine)
        text = _run(m.build_override_details_context(None, coord))
        assert "duration 30 min (ongoing)" in text

    def test_active_override_unknown_start_and_bad_time(self):
        engine = _engine()
        engine._manual_override_active = True
        engine._grace_active = True
        coord = SimpleNamespace(_today_record=None, config={}, automation_engine=engine)
        assert "Current override:  active (start time unknown)" in _run(m.build_override_details_context(None, coord))
        engine._manual_override_time = "garbage"
        assert "Current override:  active (duration unknown)" in _run(m.build_override_details_context(None, coord))

    @pytest.mark.parametrize("aware", [True, False])
    def test_stuck_grace_is_flagged(self, aware):
        grace_end = _now() - datetime.timedelta(hours=1)
        engine = _engine()
        engine._manual_override_active = True
        engine._manual_override_time = _now().isoformat()
        engine._grace_end_time = (grace_end if aware else grace_end.replace(tzinfo=None)).isoformat()
        coord = SimpleNamespace(_today_record=None, config={}, automation_engine=engine)
        assert "WARNING STUCK GRACE DETECTED" in _run(m.build_override_details_context(None, coord))

    def test_future_or_unreadable_grace_end_is_not_flagged(self):
        engine = _engine()
        engine._manual_override_active = True
        engine._manual_override_time = _now().isoformat()
        coord = SimpleNamespace(_today_record=None, config={}, automation_engine=engine)
        for grace_end in ((_now() + datetime.timedelta(hours=1)).isoformat(), "garbage"):
            engine._grace_end_time = grace_end
            assert "STUCK GRACE" not in _run(m.build_override_details_context(None, coord))

    def test_unreadable_override_record_is_unavailable(self):
        coord = SimpleNamespace(_today_record=SimpleNamespace(manual_overrides=1, override_details=5))
        text = _run(m.build_override_details_context(None, coord))
        assert text.startswith("=== MANUAL OVERRIDES TODAY ===\n  (unavailable)\n")

    def test_fan_ownership_history(self):
        events = [
            {"type": "fan_activated", "time": _ago(60)},
            {"type": "nat_vent_fan_on", "time": _ago(59)},  # CA already owns it: no new line
            {"type": "nat_vent_comfort_floor_exit", "time": _ago(58)},
            {"type": "fan_cancel", "time": _ago(57)},  # nobody owns it: no line
            {"type": "fan_manual_override", "fan_after": "on", "time": _ago(56)},
            {"type": "fan_manual_override", "fan_after": "off", "time": _ago(55)},
            {"type": "fan_manual_override", "fan_after": "on", "time": _ago(54)},
            {"type": "fan_cancel", "time": _ago(53)},
            *({"type": "noise", "time": _ago(10)} for _ in range(200)),
        ]
        coord = SimpleNamespace(_today_record=None, config={}, automation_engine=None, _event_log=events)
        text = _run(m.build_override_details_context(None, coord, hours=24))
        ownership = text.split("=== FAN OWNERSHIP HISTORY ===")[1]
        assert "  Fan override count (window): 0" in ownership
        assert "NOTE: window contains more than 200 events" in ownership

        coord._event_log = events[:8]
        ownership = _run(m.build_override_details_context(None, coord, hours=24)).split("FAN OWNERSHIP HISTORY")[1]
        assert ownership.count("CA owns fan") == 1
        assert "CA released fan (nat_vent_comfort_floor_exit)" in ownership
        assert ownership.count("User owns fan") == 2
        assert "Fan ownership cleared (fan_manual_override, fan->off)" in ownership
        assert "Fan ownership cleared (fan_cancel)" in ownership
        assert "  Fan override count (window): 2" in ownership

    def test_bad_hours_skips_fan_history(self):
        coord = SimpleNamespace(_today_record=None, config={}, automation_engine=None, _event_log=[])
        assert "FAN OWNERSHIP" not in _run(m.build_override_details_context(None, coord, hours="abc"))


def test_session_grouping_with_no_events():
    assert m._group_timeline_sessions([], {}, 24, _now()) == ([], False)
