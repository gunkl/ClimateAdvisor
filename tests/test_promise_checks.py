"""Tests for the household-promise checks (Issue #1128, Step 3).

Every promise has a NEGATIVE CONTROL (a minimal history rebuilt from the bug that motivated it,
which must produce a Hit) and a COMPLIANT TWIN (the closest correct behavior, which must not).
Inputs are constructed from evidence records; outcomes are asserted. No production logic is
re-implemented here (DOCTRINE: never mirror the logic under test).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import pytest

if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

from custom_components.climate_advisor.const import FAN_MODE_HVAC, FAN_MODE_WHOLE_HOUSE  # noqa: E402
from custom_components.climate_advisor.invariant_watchdog import (  # noqa: E402
    LEVEL_ALERT,
    PROMISES,
    SCOPE_HOME,
    SCOPE_TEST,
    SEVERITY_HARD,
    SEVERITY_WARNING,
    PromiseConfig,
    equipment,
    occupant_actions,
    run_promise_checks,
)
from custom_components.climate_advisor.promise_journal import (  # noqa: E402
    Command,
    PromiseInput,
    PromiseJournal,
    Reading,
    derive_episodes,
    mark_from_event,
)

T0 = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def rd(minutes: float, **kw) -> Reading:
    base = {
        "hvac_mode": "cool",
        "hvac_action": "idle",
        "indoor_f": 72.0,
        "outdoor_f": 80.0,
        "forecast_high_f": 82.0,
        "setpoint_f": 74.0,
        "fan_on": False,
    }
    base.update(kw)
    return Reading(ts=at(minutes), **base)


def cmd(minutes: float, **kw) -> Command:
    base = {"target": "thermostat", "service": "set_hvac_mode", "origin": "ca", "indoor_f": 70.0}
    base.update(kw)
    return Command(ts=at(minutes), **base)


def events(*items):
    """(minutes, type, payload) -> marks."""
    return [mark_from_event(at(m), t, p) for m, t, p in items]


def make_input(readings=(), commands=(), marks=(), now=None) -> PromiseInput:
    readings = tuple(sorted(readings, key=lambda r: r.ts))
    marks = tuple(sorted(marks, key=lambda m: m.ts))
    if now is None:
        stamps = [r.ts for r in readings] + [c.ts for c in commands] + [m.ts for m in marks]
        now = max(stamps) if stamps else T0
    return PromiseInput(
        now=now,
        latest=readings[-1] if readings else None,
        readings=readings,
        commands=tuple(commands),
        marks=marks,
        episodes=derive_episodes(marks, readings),
    )


def hits_for(key: str, inp: PromiseInput, cfg: PromiseConfig | None = None, scope: str = SCOPE_TEST):
    return [h for h in run_promise_checks(inp, cfg or PromiseConfig(), scope=scope) if h.promise == key]


# ─────────────────────────────────────────────────────────────────────────────
# Registry and runner
# ─────────────────────────────────────────────────────────────────────────────
class TestRegistry:
    def test_every_promise_has_a_unique_key_and_a_known_severity(self):
        keys = [p.key for p in PROMISES]
        assert len(keys) == len(set(keys))
        assert {p.test_severity for p in PROMISES} <= {SEVERITY_HARD, SEVERITY_WARNING}

    def test_promise_five_has_a_second_clause(self):
        assert [p.label for p in PROMISES if p.number == 5] == ["5", "5b"]

    def test_only_the_mutex_alerts_at_home_today(self):
        assert [p.key for p in PROMISES if p.home_level == LEVEL_ALERT] == ["ac_whf_mutex"]

    def test_unknown_scope_is_rejected(self):
        with pytest.raises(ValueError):
            run_promise_checks(make_input(), PromiseConfig(), scope="elsewhere")

    def test_empty_input_reports_nothing(self):
        assert run_promise_checks(make_input(), PromiseConfig(), scope=SCOPE_TEST) == []

    def test_a_crashing_check_is_swallowed_at_home_but_raised_in_tests(self, monkeypatch):
        import custom_components.climate_advisor.invariant_watchdog as w

        def boom(inp, cfg):
            raise RuntimeError("broken check")

        spec = w.PromiseSpec(99, "boom", "99", boom, SEVERITY_HARD, w.LEVEL_LOG)
        monkeypatch.setattr(w, "PROMISES", (spec,))
        assert w.run_promise_checks(make_input(), PromiseConfig(), scope=SCOPE_HOME) == []
        with pytest.raises(RuntimeError):
            w.run_promise_checks(make_input(), PromiseConfig(), scope=SCOPE_TEST)

    def test_real_journal_window_feeds_the_checks_end_to_end(self):
        j = PromiseJournal()
        j.record_reading(rd(0, hvac_action="cooling", fan_on=True))
        w = j.window(at(1))
        hits = run_promise_checks(w, PromiseConfig(fan_mode=FAN_MODE_WHOLE_HOUSE), scope=SCOPE_HOME)
        assert [h.promise for h in hits] == ["ac_whf_mutex"]


class TestDerivedHelpers:
    def test_equipment_uses_reported_action(self):
        assert equipment(rd(0, hvac_action="heating")) == "heating"
        assert equipment(rd(0, hvac_action="off")) == "idle"
        assert equipment(rd(0, hvac_action="drying")) is None

    def test_equipment_infers_when_action_is_missing(self):
        r = rd(0, hvac_action=None, hvac_mode="heat", indoor_f=66.0, setpoint_f=68.0)
        assert equipment(r) == "heating"
        assert equipment(rd(0, hvac_action=None, hvac_mode="heat", indoor_f=68.2, setpoint_f=68.0)) == "idle"
        assert equipment(rd(0, hvac_action=None, hvac_mode=None)) is None

    def test_occupant_action_is_a_change_with_no_nearby_ca_command(self):
        rs = [rd(0), rd(10, setpoint_f=70.0)]
        assert [(o.target, o.what) for o in occupant_actions(make_input(rs))] == [("thermostat", "setpoint")]

    def test_a_ca_command_just_before_the_change_explains_it(self):
        rs = [rd(0), rd(10, setpoint_f=70.0)]
        inp = make_input(rs, [cmd(10, service="set_temperature") if False else cmd(9.6)])
        assert occupant_actions(inp) == []

    def test_a_ca_command_to_the_other_device_does_not_explain_it(self):
        rs = [rd(0), rd(10, fan_on=True)]
        inp = make_input(rs, [cmd(9.9, target="thermostat")])
        assert [(o.target) for o in occupant_actions(inp)] == ["fan"]


# ─────────────────────────────────────────────────────────────────────────────
# P1 settling_flip  (past bugs: #821-#867, #1066, #1071, #1073)
# ─────────────────────────────────────────────────────────────────────────────
def _afternoon_cooling_then_heat(*, indoor, forecast=82.0, setpoint=68.0, with_cooling=True, ca_command=True):
    """AC cooled the house this afternoon; at 15:10 the thermostat goes to heat at the floor.

    ``indoor`` is the temperature when heating starts (comfort floor 68 F by default).
    """
    rs = [
        rd(0, hvac_action="cooling" if with_cooling else "idle", indoor_f=75.0, forecast_high_f=forecast),
        rd(20, hvac_action="idle", indoor_f=72.5, forecast_high_f=forecast),
        rd(
            70,
            hvac_mode="heat",
            hvac_action="heating",
            indoor_f=indoor,
            setpoint_f=setpoint,
            forecast_high_f=forecast,
        ),
    ]
    cs = [cmd(69.97, mode="heat")] if ca_command else []
    return make_input(rs, cs)


class TestP1SettlingFlip:
    def test_negative_control_1073_heat_on_a_shallow_dip_right_after_cooling(self):
        """#1073: the furnace started 1 F under the floor 50 min after the AC had cooled the house."""
        (h,) = hits_for("settling_flip", _afternoon_cooling_then_heat(indoor=67.0))
        assert dict(h.evidence)["side"] == "heat"
        assert dict(h.evidence)["dip_f"] == pytest.approx(1.0)
        assert "should have waited" in h.detail and "heat started" in h.detail

    def test_twin_deep_dip_is_a_legitimate_furnace_start(self):
        assert hits_for("settling_flip", _afternoon_cooling_then_heat(indoor=65.0)) == []

    def test_twin_dip_exactly_at_the_default_tolerance_is_not_flagged(self):
        assert hits_for("settling_flip", _afternoon_cooling_then_heat(indoor=66.0)) == []

    def test_twin_no_cooling_in_the_window(self):
        assert hits_for("settling_flip", _afternoon_cooling_then_heat(indoor=67.0, with_cooling=False)) == []

    def test_twin_cooling_older_than_the_recency_window(self):
        rs = [
            rd(0, hvac_action="cooling", indoor_f=75.0),
            rd(30, hvac_action="idle", indoor_f=72.0),
            rd(160, hvac_mode="heat", hvac_action="heating", indoor_f=67.0, setpoint_f=68.0),
        ]
        assert hits_for("settling_flip", make_input(rs, [cmd(159.9, mode="heat")])) == []

    def test_twin_cool_native_day_is_exempt(self):
        """On a cold-forecast day heat is the day's natural side; production never holds it."""
        assert hits_for("settling_flip", _afternoon_cooling_then_heat(indoor=67.0, forecast=45.0)) == []

    def test_twin_unknown_forecast_is_silent(self):
        assert hits_for("settling_flip", _afternoon_cooling_then_heat(indoor=67.0, forecast=None)) == []

    def test_twin_setpoint_above_the_floor_is_a_deliberate_bank_not_a_floor_defense(self):
        assert hits_for("settling_flip", _afternoon_cooling_then_heat(indoor=67.0, setpoint=71.0)) == []

    def test_twin_occupant_started_the_heat(self):
        """No CA command near the mode change: the occupant did it, so it is not CA's flip."""
        assert hits_for("settling_flip", _afternoon_cooling_then_heat(indoor=67.0, ca_command=False)) == []

    def test_twin_open_thermostat_override_excuses_it(self):
        base = _afternoon_cooling_then_heat(indoor=67.0)
        marks = events((60, "override_detected", {"detected_mode": "heat"}))
        inp = make_input(base.readings, base.commands, marks)
        assert hits_for("settling_flip", inp) == []

    def test_shallow_zone_follows_a_configured_tight_tolerance(self):
        cfg = PromiseConfig.from_config(
            {f"comfort_deadband_{d}_f": 1.0 for d in ("hot", "warm", "mild", "cool", "cold")}
        )
        assert hits_for("settling_flip", _afternoon_cooling_then_heat(indoor=67.0), cfg) == []
        assert len(hits_for("settling_flip", _afternoon_cooling_then_heat(indoor=67.8), cfg)) == 1

    def test_mirror_cool_clause_ac_on_a_shallow_overshoot_right_after_heat(self):
        """M1: the settling guard is symmetric; AC starting 1 F over the ceiling just after heat is the same bug."""
        rs = [
            rd(0, hvac_mode="heat", hvac_action="heating", indoor_f=66.0, setpoint_f=68.0, forecast_high_f=50.0),
            rd(20, hvac_mode="heat", hvac_action="idle", indoor_f=70.0, forecast_high_f=50.0),
            rd(70, hvac_mode="cool", hvac_action="cooling", indoor_f=75.0, setpoint_f=74.0, forecast_high_f=50.0),
        ]
        (h,) = hits_for("settling_flip", make_input(rs, [cmd(69.97, mode="cool")]))
        assert dict(h.evidence)["side"] == "cool" and "AC started" in h.detail

    def test_mirror_twin_hot_day_is_cool_native(self):
        rs = [
            rd(0, hvac_mode="heat", hvac_action="heating", indoor_f=66.0, setpoint_f=68.0, forecast_high_f=95.0),
            rd(20, hvac_mode="heat", hvac_action="idle", indoor_f=70.0, forecast_high_f=95.0),
            rd(70, hvac_mode="cool", hvac_action="cooling", indoor_f=75.0, setpoint_f=74.0, forecast_high_f=95.0),
        ]
        assert hits_for("settling_flip", make_input(rs, [cmd(69.97, mode="cool")])) == []

    def test_sleep_window_uses_the_sleep_floor(self):
        """At 02:00 the floor is the sleep floor (64), so 66 F is not a dip at all."""
        cfg = PromiseConfig.from_config({"sleep_time": "22:00", "wake_time": "06:00", "sleep_heat": 64.0})
        base = datetime(2026, 7, 16, 0, 0, tzinfo=UTC)
        rs = [
            Reading(base, "cool", "cooling", 75.0, 70.0, 82.0, 72.0, False),
            Reading(base + timedelta(minutes=20), "cool", "idle", 70.0, 70.0, 82.0, 72.0, False),
            Reading(base + timedelta(minutes=120), "heat", "heating", 66.0, 70.0, 82.0, 64.0, False),
        ]
        inp = make_input(rs, [Command(ts=base + timedelta(minutes=119, seconds=58), target="thermostat", service="x")])
        assert hits_for("settling_flip", inp, cfg) == []


# ─────────────────────────────────────────────────────────────────────────────
# P2 ac_whf_mutex  (#739, #748, #1057)
# ─────────────────────────────────────────────────────────────────────────────
class TestP2AcWhfMutex:
    WHF = PromiseConfig(fan_mode=FAN_MODE_WHOLE_HOUSE)

    def test_negative_control_ac_cooling_while_whf_runs(self):
        inp = make_input([rd(0, hvac_action="cooling", fan_on=True)])
        (h,) = hits_for("ac_whf_mutex", inp, self.WHF)
        assert h.kind == "state" and "must never run at the same time" in h.detail

    def test_twin_ac_idle(self):
        assert hits_for("ac_whf_mutex", make_input([rd(0, hvac_action="idle", fan_on=True)]), self.WHF) == []

    def test_twin_hvac_fan_archetype_coexists_with_the_compressor(self):
        inp = make_input([rd(0, hvac_action="cooling", fan_on=True)])
        assert hits_for("ac_whf_mutex", inp, PromiseConfig(fan_mode=FAN_MODE_HVAC)) == []

    def test_twin_unknown_fan_state(self):
        assert hits_for("ac_whf_mutex", make_input([rd(0, hvac_action="cooling", fan_on=None)]), self.WHF) == []

    def test_min_overlap_requires_a_sustained_overlap(self):
        cfg = PromiseConfig(fan_mode=FAN_MODE_WHOLE_HOUSE, p2_min_overlap_s=120.0)
        blip = [rd(0, hvac_action="idle", fan_on=True), rd(1, hvac_action="cooling", fan_on=True)]
        assert hits_for("ac_whf_mutex", make_input(blip), cfg) == []
        sustained = [
            rd(0, hvac_action="cooling", fan_on=True),
            rd(1, hvac_action="cooling", fan_on=True),
            rd(2.5, hvac_action="cooling", fan_on=True),
        ]
        assert len(hits_for("ac_whf_mutex", make_input(sustained), cfg)) == 1

    def test_only_the_latest_reading_matters(self):
        rs = [rd(0, hvac_action="cooling", fan_on=True), rd(5, hvac_action="idle", fan_on=True)]
        assert hits_for("ac_whf_mutex", make_input(rs), self.WHF) == []


# ─────────────────────────────────────────────────────────────────────────────
# P3 observe_only_silence  (#1108; family #589, #1102, #1109)
# ─────────────────────────────────────────────────────────────────────────────
class TestP3ObserveOnlySilence:
    def test_negative_control_1108_fan_off_sent_while_automation_was_off(self):
        c = cmd(0, target="fan", service="turn_off", fan_on=False, automation_enabled=False)
        (h,) = hits_for("observe_only_silence", make_input(commands=[c]))
        assert "switched off" in h.detail

    def test_thermostat_command_while_off_is_also_a_hit(self):
        c = cmd(0, service="set_temperature", automation_enabled=False)
        assert len(hits_for("observe_only_silence", make_input(commands=[c]))) == 1

    def test_twin_same_command_with_automation_on(self):
        c = cmd(0, target="fan", service="turn_off", fan_on=False, automation_enabled=True)
        assert hits_for("observe_only_silence", make_input(commands=[c])) == []

    def test_twin_occupant_command_while_off_is_not_ca(self):
        c = cmd(0, target="fan", service="turn_off", origin="external", automation_enabled=False)
        assert hits_for("observe_only_silence", make_input(commands=[c])) == []

    def test_two_commands_have_distinct_keys(self):
        cs = [cmd(0, automation_enabled=False), cmd(1, automation_enabled=False)]
        hits = hits_for("observe_only_silence", make_input(commands=cs))
        assert len({h.key for h in hits}) == 2


# ─────────────────────────────────────────────────────────────────────────────
# P4 override_respected  (#1027, #1045, #1055, #1006, #1119)
# ─────────────────────────────────────────────────────────────────────────────
def _thermostat_override(extra_events=(), commands=(), readings=None):
    marks = events(
        (0, "override_detected", {"detected_mode": "heat"}),
        (0, "grace_started", {"source": "manual", "trigger": "manual", "duration_seconds": 1800}),
        *extra_events,
    )
    rs = readings or [rd(-1, hvac_mode="cool"), rd(0, hvac_mode="heat", hvac_action="heating", setpoint_f=70.0)]
    return make_input(rs, commands, marks, now=at(40))


class TestP4OverrideRespected:
    def test_negative_control_1119_wakeup_switched_heat_off_inside_the_override(self):
        """#1119: the occupant set heat, then the wake-up routine switched HVAC off through an open window."""
        inp = _thermostat_override(commands=[cmd(5, mode="off")])
        (h,) = hits_for("override_respected", inp)
        assert dict(h.evidence)["reversed_to"] == "off"
        assert "before your manual change was due to end" in h.detail

    def test_twin_re_affirming_the_occupants_mode_is_not_a_reversal(self):
        assert hits_for("override_respected", _thermostat_override(commands=[cmd(5, mode="heat")])) == []

    def test_twin_command_after_natural_grace_expiry(self):
        inp = _thermostat_override(extra_events=[(30, "grace_expired", {})], commands=[cmd(31, mode="off")])
        assert hits_for("override_respected", inp) == []

    def test_twin_dashboard_end_grace_button_excuses_the_command(self):
        extra = [(5, "override_cleared", {"was_mode": "heat", "reason": "user_cancel_override"})]
        inp = _thermostat_override(extra_events=extra, commands=[cmd(5, mode="off")])
        assert hits_for("override_respected", inp) == []

    def test_twin_occupancy_away_excuses_it(self):
        extra = [(5, "override_cleared", {"was_mode": "heat", "reason": "occupancy_away"})]
        inp = _thermostat_override(extra_events=extra, commands=[cmd(5, mode="off")])
        assert hits_for("override_respected", inp) == []

    def test_twin_a_newer_occupant_action_excuses_it(self):
        rs = [
            rd(-1, hvac_mode="cool"),
            rd(0, hvac_mode="heat", hvac_action="heating", setpoint_f=70.0),
            rd(3, hvac_mode="cool", hvac_action="cooling", setpoint_f=70.0),  # the occupant again, no CA command
        ]
        inp = _thermostat_override(commands=[cmd(5, mode="cool")], readings=rs)
        assert hits_for("override_respected", inp) == []

    def test_bedtime_ending_a_thermostat_override_early_is_a_violation(self):
        """K3: bedtime/wake-up only clear FAN overrides; a thermostat override must survive them."""
        extra = [(10, "override_cleared", {"was_mode": "heat", "reason": "bedtime"})]
        hits = hits_for("override_respected", _thermostat_override(extra_events=extra))
        assert [h.key.split(":")[1] for h in hits] == ["early_end"]

    def test_bedtime_then_a_reversing_command_inside_the_remaining_grace_is_also_caught(self):
        extra = [(10, "override_cleared", {"was_mode": "heat", "reason": "bedtime"})]
        inp = _thermostat_override(extra_events=extra, commands=[cmd(10.1, mode="off")])
        assert len(hits_for("override_respected", inp)) == 2

    def test_unlisted_end_with_less_than_a_minute_of_grace_left_is_tolerated(self):
        extra = [(29.5, "override_cleared", {"was_mode": "heat", "reason": "bedtime"})]
        assert hits_for("override_respected", _thermostat_override(extra_events=extra)) == []

    def test_stuck_grace_recovery_killing_a_protected_grace_is_not_excused(self):
        extra = [(10, "stuck_grace_recovered", {"reason": "orphan"})]
        assert len(hits_for("override_respected", _thermostat_override(extra_events=extra))) == 1

    def test_setpoint_reversal_without_a_mode_change(self):
        rs = [rd(-1, setpoint_f=74.0), rd(0, hvac_mode="cool", hvac_action="cooling", setpoint_f=70.0)]
        marks = events((0, "override_detected", {"detected_mode": "cool"}))
        inp = make_input(rs, [cmd(5, service="set_temperature", mode="cool", setpoint_f=75.0)], marks, now=at(10))
        assert len(hits_for("override_respected", inp)) == 1

    def test_small_setpoint_difference_is_not_a_reversal(self):
        rs = [rd(-1, setpoint_f=74.0), rd(0, hvac_mode="cool", hvac_action="cooling", setpoint_f=70.0)]
        marks = events((0, "override_detected", {"detected_mode": "cool"}))
        inp = make_input(rs, [cmd(5, service="set_temperature", mode="cool", setpoint_f=71.0)], marks, now=at(10))
        assert hits_for("override_respected", inp) == []

    # ── fan overrides: bedtime / wake-up are legitimate endings (#1027) ──
    def _fan_override(self, extra=(), commands=()):
        marks = events((0, "fan_manual_override", {"fan_before": "off", "fan_after": "on"}), *extra)
        return make_input([rd(-1, fan_on=False), rd(0, fan_on=True)], commands, marks, now=at(30))

    def test_fan_override_negative_control_ca_turned_the_occupants_fan_off(self):
        inp = self._fan_override(commands=[cmd(5, target="fan", service="turn_off", fan_on=False)])
        assert len(hits_for("override_respected", inp)) == 1

    def test_fan_override_twin_bedtime_turning_it_off_is_legitimate(self):
        extra = [(5, "override_cleared", {"was_mode": None, "reason": "bedtime"})]
        inp = self._fan_override(extra, [cmd(5, target="fan", service="turn_off", fan_on=False)])
        assert hits_for("override_respected", inp) == []

    def test_fan_override_twin_command_matching_the_occupant_is_fine(self):
        inp = self._fan_override(commands=[cmd(5, target="fan", service="turn_on", fan_on=True)])
        assert hits_for("override_respected", inp) == []

    def test_fan_off_grace_negative_control_ca_turned_the_fan_back_on(self):
        marks = events((0, "grace_started", {"trigger": "fan_off", "duration_seconds": 1800}))
        inp = make_input(
            [rd(0, fan_on=False)], [cmd(5, target="fan", service="turn_on", fan_on=True)], marks, now=at(10)
        )
        assert len(hits_for("override_respected", inp)) == 1

    def test_fan_off_grace_twin_bedtime_does_not_excuse_it(self):
        marks = events(
            (0, "grace_started", {"trigger": "fan_off", "duration_seconds": 1800}),
            (5, "grace_cancelled", {"reason": "bedtime", "trigger": "fan_off"}),
        )
        inp = make_input([rd(0, fan_on=False)], [], marks, now=at(10))
        assert [h.key.split(":")[1] for h in hits_for("override_respected", inp)] == ["early_end"]


# ─────────────────────────────────────────────────────────────────────────────
# P5 / P5b blind fan  (#1042, #1037, #1043)
# ─────────────────────────────────────────────────────────────────────────────
class TestP5BlindFan:
    def test_negative_control_1042_economizer_started_the_fan_with_indoor_unreadable(self):
        c = cmd(0, target="fan", service="turn_on", fan_on=True, indoor_f=None)
        (h,) = hits_for("no_blind_fan_start", make_input(commands=[c]))
        assert "could not be read" in h.detail

    def test_twin_same_start_with_a_readable_indoor(self):
        c = cmd(0, target="fan", service="turn_on", fan_on=True, indoor_f=71.0)
        assert hits_for("no_blind_fan_start", make_input(commands=[c])) == []

    def test_blind_stop_is_fine(self):
        c = cmd(0, target="fan", service="turn_off", fan_on=False, indoor_f=None)
        assert hits_for("no_blind_fan_start", make_input(commands=[c])) == []

    def test_thermostat_fan_mode_on_counts_as_a_start(self):
        c = cmd(0, service="set_fan_mode", mode="on", indoor_f=None)
        assert len(hits_for("no_blind_fan_start", make_input(commands=[c]))) == 1

    def test_occupant_start_is_not_ca(self):
        c = cmd(0, target="fan", service="turn_on", fan_on=True, indoor_f=None, origin="external")
        assert hits_for("no_blind_fan_start", make_input(commands=[c])) == []

    def _blind_run(self, minutes, *, ongoing_until=None):
        rs = [rd(m, fan_on=True, indoor_f=None) for m in range(0, minutes + 1, 5)]
        return make_input(rs, now=at(ongoing_until if ongoing_until is not None else minutes))

    def test_p5b_negative_control_fan_kept_running_blind_for_forty_minutes(self):
        (h,) = hits_for("blind_fan_continue", self._blind_run(40))
        assert dict(h.evidence)["minutes"] == pytest.approx(40.0)

    def test_p5b_twin_thirty_minutes_matches_production_auto_stop(self):
        assert hits_for("blind_fan_continue", self._blind_run(30)) == []

    def test_p5b_an_ongoing_run_counts_up_to_now(self):
        assert len(hits_for("blind_fan_continue", self._blind_run(10, ongoing_until=50))) == 1

    def test_p5b_twin_indoor_comes_back(self):
        rs = [rd(0, fan_on=True, indoor_f=None), rd(20, fan_on=True, indoor_f=71.0), rd(50, fan_on=True, indoor_f=None)]
        assert hits_for("blind_fan_continue", make_input(rs, now=at(60))) == []

    def test_p5b_twin_fan_off(self):
        rs = [rd(m, fan_on=False, indoor_f=None) for m in range(0, 60, 5)]
        assert hits_for("blind_fan_continue", make_input(rs)) == []


# ─────────────────────────────────────────────────────────────────────────────
# P6 mode_flip_rate  (#1066 / #1073 flip-flop)
# ─────────────────────────────────────────────────────────────────────────────
def _flips(times_modes, *, with_commands=True):
    rs, cs = [], []
    for m, mode in times_modes:
        rs.append(rd(m, hvac_mode=mode))
        if with_commands:
            cs.append(cmd(m - 0.02, mode=mode))
    return make_input(rs, cs)


class TestP6ModeFlipRate:
    def test_negative_control_three_flips_in_an_hour(self):
        inp = _flips([(0, "heat"), (10, "cool"), (20, "heat"), (30, "cool")])
        (h,) = hits_for("mode_flip_rate", inp)
        assert dict(h.evidence)["flips"] == 3

    def test_twin_two_flips_in_an_hour(self):
        assert hits_for("mode_flip_rate", _flips([(0, "heat"), (10, "cool"), (20, "heat")])) == []

    def test_twin_flips_spread_over_more_than_an_hour(self):
        assert hits_for("mode_flip_rate", _flips([(0, "heat"), (30, "cool"), (70, "heat"), (110, "cool")])) == []

    def test_twin_occupant_flipping_the_thermostat_is_excused(self):
        inp = _flips([(0, "heat"), (10, "cool"), (20, "heat"), (30, "cool")], with_commands=False)
        assert hits_for("mode_flip_rate", inp) == []

    def test_pausing_through_off_and_returning_to_the_same_family_is_not_a_flip(self):
        inp = _flips([(0, "heat"), (10, "off"), (20, "heat"), (30, "off"), (40, "heat"), (50, "off"), (55, "heat")])
        assert hits_for("mode_flip_rate", inp) == []

    def test_heat_off_cool_is_a_flip(self):
        inp = _flips([(0, "heat"), (5, "off"), (10, "cool"), (20, "heat"), (30, "cool")])
        assert len(hits_for("mode_flip_rate", inp)) == 1


# ─────────────────────────────────────────────────────────────────────────────
# P7 comfort_kept  (#1066: the house sat past an edge while armed on the wrong side)
# ─────────────────────────────────────────────────────────────────────────────
def _cold_house(minutes, *, action="idle", indoor=64.0, **kw):
    """Readings every 10 min, indoor 64 F with the floor at 68 F (3 F past the 1 F slack)."""
    return [
        rd(m, hvac_mode="heat", hvac_action=action, indoor_f=indoor, setpoint_f=66.0, forecast_high_f=50.0, **kw)
        for m in range(0, minutes + 1, 10)
    ]


def _ca_heat_at_start():
    """CA itself switched cool -> heat when the cold stretch begins, so it is not an occupant action."""
    return cmd(-0.02, mode="heat")


class TestP7ComfortKept:
    def test_negative_control_house_sat_below_the_floor_while_ca_was_in_control(self):
        (h,) = hits_for("comfort_kept", make_input(_cold_house(60)))
        assert dict(h.evidence)["side"] == "below" and dict(h.evidence)["minutes"] > 45

    def test_twin_forty_minutes_is_inside_the_allowance(self):
        assert hits_for("comfort_kept", make_input(_cold_house(40))) == []

    def test_twin_furnace_is_running_so_it_is_just_undersized_equipment(self):
        assert hits_for("comfort_kept", make_input(_cold_house(60, action="heating"))) == []

    def test_twin_a_door_is_open(self):
        assert hits_for("comfort_kept", make_input(_cold_house(60, contacts_open=True))) == []

    def test_twin_occupant_is_away(self):
        marks = events((-5, "occupancy_setback", {"mode": "away"}))
        assert hits_for("comfort_kept", make_input(_cold_house(60), marks=marks)) == []

    def test_twin_returning_home_restores_control(self):
        marks = events((-30, "occupancy_setback", {}), (-20, "occupancy_comfort_restored", {}))
        assert len(hits_for("comfort_kept", make_input(_cold_house(60), marks=marks))) == 1

    def test_twin_manual_override_in_effect(self):
        marks = events((-1, "override_detected", {"detected_mode": "heat"}))
        inp = make_input(_cold_house(60), marks=marks, now=at(60))
        assert hits_for("comfort_kept", inp) == []

    def test_twin_the_whole_house_fan_is_running(self):
        """A running fan is ventilating on purpose; the cold stretch is not the thermostat's failure."""
        assert hits_for("comfort_kept", make_input(_cold_house(60, fan_on=True))) == []

    def test_twin_inside_an_active_nat_vent_session(self):
        marks = events((-5, "nat_vent_activated", {}))
        assert hits_for("comfort_kept", make_input(_cold_house(60), marks=marks)) == []

    def test_twin_after_the_nat_vent_session_ended_control_returns(self):
        marks = events((-30, "nat_vent_activated", {}), (-20, "nat_vent_comfort_floor_exit", {}))
        assert len(hits_for("comfort_kept", make_input(_cold_house(60), marks=marks))) == 1

    def test_twin_the_ac_ran_recently_so_a_settling_hold_explains_the_dip(self):
        pre = [rd(-60, hvac_mode="cool", hvac_action="cooling", indoor_f=74.0)]
        assert hits_for("comfort_kept", make_input(pre + _cold_house(60), [_ca_heat_at_start()])) == []

    def test_a_big_excursion_is_not_explained_by_settling(self):
        pre = [rd(-60, hvac_mode="cool", hvac_action="cooling", indoor_f=74.0)]
        deep = _cold_house(60, indoor=55.0)  # 12 F below the slackened floor
        assert len(hits_for("comfort_kept", make_input(pre + deep, [_ca_heat_at_start()]))) == 1

    def test_twin_within_the_range_slack(self):
        assert hits_for("comfort_kept", make_input(_cold_house(90, indoor=67.2))) == []

    def test_twin_automation_switched_off(self):
        c = cmd(0, service="set_hvac_mode", automation_enabled=False)
        assert hits_for("comfort_kept", make_input(_cold_house(60), [c])) == []

    def test_twin_just_after_a_restart(self):
        marks = events((-5, "system_restarted", {}))
        assert hits_for("comfort_kept", make_input(_cold_house(60), marks=marks, now=at(60))) == []

    def test_twin_a_gap_in_readings_breaks_the_stretch(self):
        rs = [r for r in _cold_house(120) if not (40 <= (r.ts - T0).total_seconds() / 60 <= 80)]
        assert hits_for("comfort_kept", make_input(rs)) == []

    def test_aggressive_savings_widens_the_range(self):
        cfg = PromiseConfig.from_config({"aggressive_savings": True})
        assert hits_for("comfort_kept", make_input(_cold_house(60, indoor=66.0)), cfg) == []
        assert len(hits_for("comfort_kept", make_input(_cold_house(60, indoor=66.0)))) == 1
        assert len(hits_for("comfort_kept", make_input(_cold_house(60, indoor=64.0)), cfg)) == 1

    def test_hot_side_is_symmetric(self):
        rs = [rd(m, hvac_mode="cool", hvac_action="idle", indoor_f=79.0, setpoint_f=74.0) for m in range(0, 61, 10)]
        (h,) = hits_for("comfort_kept", make_input(rs))
        assert dict(h.evidence)["side"] == "above"

    def test_celsius_user_sees_the_excess_in_celsius_but_evidence_stays_fahrenheit(self):
        cfg = PromiseConfig.from_config({"temp_unit": "celsius"})
        (h,) = hits_for("comfort_kept", make_input(_cold_house(60)), cfg)
        assert dict(h.evidence)["worst_f"] == pytest.approx(3.0)
        assert "1.7" in h.detail  # 3 F expressed as a delta in C


class TestPromiseConfig:
    def test_malformed_values_fall_back_to_defaults(self):
        cfg = PromiseConfig.from_config({"comfort_heat": "warm", "sleep_time": "late", "wake_time": 7})
        assert cfg.comfort_heat == 68.0 and cfg.sleep_minute is None and cfg.wake_minute is None

    def test_non_dict_config_is_safe(self):
        assert PromiseConfig.from_config(None).fan_mode == "disabled"  # type: ignore[arg-type]

    def test_sleep_window_wraps_midnight(self):
        cfg = PromiseConfig.from_config({"sleep_time": "22:30", "wake_time": "06:30"})
        assert cfg.in_sleep(datetime(2026, 7, 15, 23, 0)) and cfg.in_sleep(datetime(2026, 7, 15, 3, 0))
        assert not cfg.in_sleep(datetime(2026, 7, 15, 12, 0))

    def test_night_owl_schedule_is_supported(self):
        cfg = PromiseConfig.from_config({"sleep_time": "02:00", "wake_time": "10:00"})
        assert cfg.in_sleep(datetime(2026, 7, 15, 5, 0)) and not cfg.in_sleep(datetime(2026, 7, 15, 23, 0))

    def test_transition_takes_the_extreme_that_keeps_each_check_looser(self):
        cfg = PromiseConfig.from_config(
            {
                "sleep_time": "22:00",
                "wake_time": "06:00",
                "comfort_heat": 70,
                "sleep_heat": 64,
                "comfort_cool": 74,
                "sleep_cool": 78,
            }
        )
        t = datetime(2026, 7, 15, 21, 30)  # inside the hour before bedtime
        assert cfg.floor_hi(t) == 70 and cfg.floor_lo(t) == 64
        assert cfg.ceiling_lo(t) == 74 and cfg.ceiling_hi(t) == 78
