"""Issue #1048 change 9 -- "house off-target for hours with no equipment running" alert.

Occupant outcome: on Oct 4 the reporter's house sat 2-3 degC below the comfort band for
hours with the thermostat Off and nothing running, and nobody was told.  The stall alert
raises one push (not email) once the house has been more than 1 degF outside the *active*
band for 3 h with no heating/cooling equipment run in the needed direction, repeats every
12 h, and reports recovery.  Never persisted.

Covers the pure decision (``decide_comfort_stall``) as a table, then the real coordinator
shell (``_update_comfort_stall`` / ``_note_equipment_activity`` /
``_compute_hvac_control_issues``) bound onto a partial coordinator -- no re-implemented logic.
"""

from __future__ import annotations

import importlib
import inspect
import sys
import types
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.comfort_stall_decision import (  # noqa: E402
    StallReason,
    decide_comfort_stall,
    stall_margins,
)

T0 = datetime(2026, 10, 4, 11, 0, tzinfo=UTC)
SUSTAIN = 3 * 3600


def _decide(now=T0, **over):
    args = {
        "indoor": 62.0,
        "comfort_heat": 68.0,
        "comfort_cool": 75.0,
        "now": now,
        "heat_margin_f": 1.0,
        "cool_margin_f": 1.0,
        "sustain_seconds": SUSTAIN,
        "previous_direction": None,
        "previous_since": None,
        "last_active": {},
        "thermostat_available": True,
        "has_hvac_action": True,
        "hvac_modes": ["off", "heat", "cool"],
        "manual_control": False,
        "sensor_open": False,
        "startup_coalescing": False,
        "nat_vent_tolerated": False,
    }
    args.update(over)
    return decide_comfort_stall(**args)


# ---------------------------------------------------------------------------
# Pure decision table
# ---------------------------------------------------------------------------


class TestDecideComfortStall:
    def test_inside_margin_is_in_band(self):
        d = _decide(indoor=67.5)  # 0.5 F below heat edge, within the 1 F margin
        assert d.reason is StallReason.IN_BAND and d.direction is None and not d.confirmed

    def test_exactly_at_margin_is_in_band(self):
        assert _decide(indoor=67.0).reason is StallReason.IN_BAND  # not MORE than 1 F outside

    def test_below_band_starts_heat_candidate(self):
        d = _decide(indoor=62.0)
        assert (d.direction, d.since, d.reason, d.confirmed) == ("heat", T0, StallReason.PENDING, False)

    def test_above_band_starts_cool_candidate(self):
        d = _decide(indoor=80.0)
        assert d.direction == "cool" and d.reason is StallReason.PENDING

    def test_confirmed_after_sustain_and_not_before(self):
        before = _decide(now=T0 + timedelta(seconds=SUSTAIN - 1), previous_direction="heat", previous_since=T0)
        after = _decide(now=T0 + timedelta(seconds=SUSTAIN), previous_direction="heat", previous_since=T0)
        assert not before.confirmed and before.reason is StallReason.PENDING
        assert after.confirmed and after.reason is StallReason.STALLED

    def test_equipment_run_in_needed_direction_restarts_the_count(self):
        ran = T0 + timedelta(hours=2)
        d = _decide(
            now=T0 + timedelta(hours=3), previous_direction="heat", previous_since=T0, last_active={"heat": ran}
        )
        assert not d.confirmed and d.effective_since == ran
        d2 = _decide(
            now=ran + timedelta(seconds=SUSTAIN),
            previous_direction="heat",
            previous_since=T0,
            last_active={"heat": ran},
        )
        assert d2.confirmed

    def test_equipment_in_the_other_direction_does_not_count(self):
        d = _decide(
            now=T0 + timedelta(hours=3),
            previous_direction="heat",
            previous_since=T0,
            last_active={"cool": T0 + timedelta(hours=2)},
        )
        assert d.confirmed

    def test_equipment_run_before_the_candidate_started_is_irrelevant(self):
        d = _decide(
            now=T0 + timedelta(hours=3),
            previous_direction="heat",
            previous_since=T0,
            last_active={"heat": T0 - timedelta(hours=1)},
        )
        assert d.confirmed and d.effective_since == T0

    def test_direction_switch_restarts_the_clock(self):
        d = _decide(indoor=80.0, now=T0 + timedelta(hours=5), previous_direction="heat", previous_since=T0)
        assert d.direction == "cool" and d.since == T0 + timedelta(hours=5) and not d.confirmed

    def test_leaving_the_band_clears_the_candidate(self):
        d = _decide(indoor=70.0, previous_direction="heat", previous_since=T0)
        assert d.direction is None and d.since is None and d.reason is StallReason.IN_BAND

    def test_each_exclusion_resets_the_clock(self):
        cases = {
            "thermostat_unavailable": {"thermostat_available": False},
            "no_hvac_action": {"has_hvac_action": False},
            "manual_control": {"manual_control": True},
            "sensor_open": {"sensor_open": True},
            "startup_coalescing": {"startup_coalescing": True},
            "nat_vent_tolerated": {"nat_vent_tolerated": True},
            "indoor_unavailable": {"indoor": None},
            "mode_unsupported": {"hvac_modes": ["off", "cool"]},
        }
        for why, over in cases.items():
            d = _decide(now=T0 + timedelta(hours=5), previous_direction="heat", previous_since=T0, **over)
            assert d.reason is StallReason.EXCLUDED, why
            assert d.excluded_by == why, why
            assert d.direction is None and d.since is None and not d.confirmed, why

    def test_mode_support_follows_direction(self):
        assert _decide(indoor=80.0, hvac_modes=["off", "heat"]).excluded_by == "mode_unsupported"
        assert _decide(indoor=62.0, hvac_modes=["off", "heat_cool"]).direction == "heat"
        assert _decide(indoor=80.0, hvac_modes=["auto"]).direction == "cool"

    def test_unknown_mode_list_does_not_exclude(self):
        assert _decide(hvac_modes=None).direction == "heat"
        assert _decide(hvac_modes=[]).direction == "heat"


# ---------------------------------------------------------------------------
# Coordinator shell
# ---------------------------------------------------------------------------


def _coordinator_class():
    return importlib.import_module("custom_components.climate_advisor.coordinator").ClimateAdvisorCoordinator


class _Rig:
    """A partial real coordinator with the real stall methods bound; time is driven by the test."""

    def __init__(self, *, unit="fahrenheit", hysteresis=1.0, modes=None, action="idle", mode="off", day_type="cold"):
        cls = _coordinator_class()
        self.coord = coord = object.__new__(cls)
        coord.config = {"temp_unit": unit, "nat_vent_hysteresis_f": hysteresis}
        ae = MagicMock()
        ae.climate_entity = "climate.thermostat"
        ae._manual_override_active = False
        ae._override_confirm_pending = False
        # the REAL predicate against this stub's flags (Issue #1133 L4: the coordinator reads it, no hand copy)
        from custom_components.climate_advisor.automation import AutomationEngine

        ae._manual_change_protected = types.MethodType(AutomationEngine._manual_change_protected, ae)
        ae._paused_by_door = False
        ae._natural_vent_active = False
        ae._hvac_write_record = None
        ae._hvac_cmd_fail_announced = None
        ae._hvac_cmd_fail_mode = None
        # Real deadband values per day type (config defaults: hot/cold 5 F, others 2 F).
        ae._comfort_deadband_for_day_type = lambda dt: 5.0 if dt in ("hot", "cold") else 2.0
        coord.automation_engine = ae
        coord._current_classification = SimpleNamespace(day_type=day_type) if day_type else None
        self.ae = ae
        coord._startup_coalesce_active = False
        self.refreshes = 0
        coord.async_request_refresh = self._count_refresh
        coord.hass = MagicMock()
        coord.hass.async_create_task = lambda coro: coro
        coord._any_sensor_open = lambda: False
        coord._event_log = []
        self.events: list[tuple[str, dict]] = []
        coord._emit_event = lambda t, d: self.events.append((t, d))
        self.notifications: list[tuple[str, str, str, dict]] = []
        coord._notify_in_background = lambda title, msg, typ, **kw: self.notifications.append((title, msg, typ, kw))
        self.set_thermostat(mode=mode, action=action, modes=modes or ["off", "heat", "cool"])
        coord.hass.states.get = lambda _eid: self.thermostat
        for name in (
            "_update_comfort_stall",
            "_note_equipment_activity",
            "_compute_hvac_control_issues",
            "_is_nat_vent_tolerated_deviation",
        ):
            setattr(coord, name, types.MethodType(getattr(cls, name), coord))
        self._patch = patch("custom_components.climate_advisor.coordinator.dt_util")
        self.dt = self._patch.start()
        self.now = T0

    def _count_refresh(self):
        self.refreshes += 1

    def set_thermostat(self, *, mode="off", action="idle", modes=None):
        attrs = {"hvac_modes": modes or ["off", "heat", "cool"]}
        if action is not None:
            attrs["hvac_action"] = action
        self.thermostat = SimpleNamespace(state=mode, attributes=attrs)

    def tick(self, minutes: float, indoor: float, heat: float = 68.0, cool: float = 75.0):
        """Advance to T0+minutes and run one stall update."""
        self.now = T0 + timedelta(minutes=minutes)
        self.dt.now.return_value = self.now
        self.coord._update_comfort_stall(indoor, heat, cool, self.now)

    def names(self):
        return [t for t, _ in self.events]

    def close(self):
        self._patch.stop()


def _rig(**kw):
    r = _Rig(**kw)
    return r


def _run_half_hours(r: _Rig, hours: float, indoor: float, **kw):
    for m in range(0, int(hours * 60) + 1, 30):
        r.tick(m, indoor, **kw)


class TestStallShell:
    def teardown_method(self):
        pass

    def test_oct4_shape_fires_once_after_three_hours_push_only(self):
        """Indoor ~5 F (~2.8 C) below the band, thermostat Off/idle: nothing before 3 h, one alert at 3 h."""
        r = _rig()
        try:
            _run_half_hours(r, 2.5, 62.0)
            assert r.events == [] and r.notifications == []
            r.tick(180, 62.0)
            assert r.names() == ["hvac_output_stall"]
            payload = r.events[0][1]
            assert payload["direction"] == "heat" and payload["hours"] == 3
            assert payload["thermostat_mode"] == "off" and payload["thermostat_action"] == "idle"
            assert len(r.notifications) == 1
            title, msg, typ, kw = r.notifications[0]
            assert typ == "hvac_output_stall" and kw == {"email": False}
            assert "below your comfort band" in msg and "heating" in msg and "3 hours" in msg
            # Later ticks within the reminder interval add nothing.
            for m in range(210, 600, 30):
                r.tick(m, 62.0)
            assert r.names() == ["hvac_output_stall"] and len(r.notifications) == 1
        finally:
            r.close()

    def test_status_entry_is_diagnostic_in_hvac_control_issues(self):
        r = _rig()
        try:
            _run_half_hours(r, 3, 62.0)
            issues = r.coord._compute_hvac_control_issues()
            stall = [i for i in issues if i["issue"] == "output_stall"]
            assert len(stall) == 1
            assert "below the comfort band" in stall[0]["detail"] and "no heating running" in stall[0]["detail"]
            assert "thermostat mode off" in stall[0]["detail"]
        finally:
            r.close()

    def test_sleep_band_night_does_not_fire(self):
        """The active band drops at night (sleep setback): the same 62 F is in-band, so no alert."""
        r = _rig()
        try:
            for m in range(0, 9 * 60, 30):
                r.tick(m, 62.0, heat=58.0, cool=80.0)  # sleep band
            assert r.events == [] and r.notifications == []
            # ...but if the daytime band applies, the clock starts only from then.
            r.tick(9 * 60, 62.0)
            assert r.events == []
        finally:
            r.close()

    def test_cooling_direction_wording(self):
        r = _rig(mode="off")
        try:
            _run_half_hours(r, 3, 82.0)
            assert r.names() == ["hvac_output_stall"] and r.events[0][1]["direction"] == "cool"
            assert "above your comfort band" in r.notifications[0][1] and "cooling" in r.notifications[0][1]
        finally:
            r.close()

    def test_celsius_text_uses_user_unit(self):
        r = _rig(unit="celsius")
        try:
            # 5.4 F below 68 F edge -> delta 3.0 C; edge 68 F -> 20 C
            _run_half_hours(r, 3, 62.6)
            msg = r.notifications[0][1]
            assert "3.0°C below" in msg and "(20°C)" in msg and "°F" not in msg
            detail = [i for i in r.coord._compute_hvac_control_issues() if i["issue"] == "output_stall"][0]["detail"]
            assert "°C" in detail and "°F" not in detail
        finally:
            r.close()

    def test_quiet_exclusions_never_alert(self):
        scenarios = {
            "manual_override": lambda r: setattr(r.ae, "_manual_override_active", True),
            "override_confirm_pending": lambda r: setattr(r.ae, "_override_confirm_pending", True),
            "door_pause": lambda r: setattr(r.ae, "_paused_by_door", True),
            "window_open": lambda r: setattr(r.coord, "_any_sensor_open", lambda: True),
            "startup_coalesce": lambda r: setattr(r.coord, "_startup_coalesce_active", True),
            "thermostat_unavailable": lambda r: r.set_thermostat(mode="unavailable"),
            "no_hvac_action": lambda r: r.set_thermostat(mode="off", action=None),
            "no_heat_mode": lambda r: r.set_thermostat(mode="off", modes=["off", "cool"]),
        }
        for name, arrange in scenarios.items():
            r = _rig()
            try:
                arrange(r)
                _run_half_hours(r, 8, 62.0)
                assert r.events == [] and r.notifications == [], name
                assert r.coord._stall_status is None, name
            finally:
                r.close()

    def test_unavailable_indoor_never_alerts(self):
        r = _rig()
        try:
            for m in range(0, 8 * 60, 30):
                r.tick(m, None)  # type: ignore[arg-type]
            assert r.events == []
        finally:
            r.close()

    def test_nat_vent_tolerated_deviation_is_quiet(self):
        r = _rig(hysteresis=3.0)
        try:
            r.ae._natural_vent_active = True
            _run_half_hours(r, 6, 66.0, heat=68.0)  # 2 F below: > margin but inside nat-vent tolerance
            assert r.events == []
        finally:
            r.close()

    def test_exclusion_ending_restarts_the_three_hours(self):
        r = _rig()
        try:
            r.ae._manual_override_active = True
            _run_half_hours(r, 4, 62.0)
            r.ae._manual_override_active = False
            r.tick(240, 62.0)
            r.tick(240 + 179, 62.0)
            assert r.events == []
            r.tick(240 + 180, 62.0)
            assert r.names() == ["hvac_output_stall"]
        finally:
            r.close()

    def test_heating_equipment_running_restarts_the_count_and_recovers(self):
        r = _rig(action="idle")
        try:
            _run_half_hours(r, 3, 62.0)
            assert r.names() == ["hvac_output_stall"]
            r.set_thermostat(mode="heat", action="heating")
            r.tick(190, 62.0)  # equipment is now running in the needed direction
            assert r.names() == ["hvac_output_stall", "hvac_output_stall_recovered"]
            assert r.events[1][1]["reason"] == "equipment_ran"
            assert r.coord._stall_status is None
        finally:
            r.close()

    def test_cooling_equipment_does_not_count_for_a_cold_house(self):
        r = _rig()
        try:
            r.set_thermostat(mode="cool", action="cooling")
            _run_half_hours(r, 3, 62.0)
            assert r.names() == ["hvac_output_stall"]
        finally:
            r.close()

    def test_short_heating_cycle_between_updates_is_remembered(self):
        """_note_equipment_activity (fed by the thermostat listener) closes the 30-min sampling gap."""
        r = _rig()
        try:
            r.tick(0, 62.0)
            r.coord._note_equipment_activity("heating", "idle", now=T0 + timedelta(minutes=100))
            for m in range(30, 181, 30):
                r.tick(m, 62.0)
            assert r.events == []  # heat ran at 100 min -> count restarts
            r.tick(100 + 180, 62.0)
            assert r.names() == ["hvac_output_stall"]
        finally:
            r.close()

    def test_recovery_when_back_in_band_once(self):
        r = _rig()
        try:
            _run_half_hours(r, 3, 62.0)
            r.tick(200, 69.0)
            r.tick(230, 69.0)
            assert r.names() == ["hvac_output_stall", "hvac_output_stall_recovered"]
            assert r.events[1][1]["reason"] == "in_band"
            assert r.coord._stall_status is None
        finally:
            r.close()

    def test_reminder_every_twelve_hours_without_new_event(self):
        r = _rig()
        try:
            _run_half_hours(r, 3, 62.0)
            for m in range(210, 180 + 12 * 60, 30):
                r.tick(m, 62.0)
            assert len(r.notifications) == 1
            r.tick(180 + 12 * 60, 62.0)
            assert len(r.notifications) == 2
            assert r.names() == ["hvac_output_stall"]
            assert r.notifications[1][3] == {"email": False}
            assert "15 hours" in r.notifications[1][1]
        finally:
            r.close()

    def test_mode_not_obeyed_episode_holds_alert_but_clock_keeps_running(self):
        r = _rig()
        try:
            r.ae._hvac_write_record = SimpleNamespace(episode_reason="heat", mismatch_since=T0)
            _run_half_hours(r, 6, 62.0)
            assert r.events == [] and r.notifications == []
            assert [i["issue"] for i in r.coord._compute_hvac_control_issues()] == ["mode_not_obeyed"]
            # Episode closes: the already-elapsed 6 h means the alert fires on the very next tick.
            r.ae._hvac_write_record = None
            r.tick(6 * 60 + 30, 62.0)
            assert r.names() == ["hvac_output_stall"]
            assert "6" in r.notifications[0][1] or "7" in r.notifications[0][1]
        finally:
            r.close()

    def test_equipment_accessor_is_per_direction_and_action_based(self):
        r = _rig()
        try:
            r.coord._note_equipment_activity("idle", "off", None, "fan", now=T0)
            assert not r.coord._equipment_last_active
            r.coord._note_equipment_activity("heating", now=T0)
            assert r.coord._equipment_last_active == {"heat": T0}
            r.coord._note_equipment_activity("COOLING", now=T0 + timedelta(minutes=1))
            assert r.coord._equipment_last_active["cool"] == T0 + timedelta(minutes=1)
            assert r.coord._equipment_last_active["heat"] == T0
        finally:
            r.close()


class TestWiring:
    def test_thermostat_listener_feeds_the_accessor_before_any_early_return(self):
        """Guard against the equipment stamp being moved after the startup-coalesce early return."""
        src = inspect.getsource(_coordinator_class()._async_thermostat_changed)
        stamp = src.index("_note_equipment_activity(")
        assert stamp < src.index("_suppress_during_startup_coalescing(")

    def test_comfort_block_calls_the_stall_update_with_the_active_band(self):
        src = inspect.getsource(_coordinator_class()._detect_and_emit_incidents)
        assert "_update_comfort_stall(indoor, comfort_heat, comfort_cool, now)" in src

    def test_events_are_registered(self):
        from custom_components.climate_advisor import ai_skills_context as ctx

        for ev in ("hvac_output_stall", "hvac_output_stall_recovered"):
            assert ev in ctx.EVENT_RENDERERS and ev in ctx._NO_DEDUP

    def test_renderers_use_the_users_unit(self):
        from custom_components.climate_advisor import ai_skills_context as ctx

        p = {"direction": "heat", "hours": 3, "indoor_f": 62.6, "edge_f": 68.0, "thermostat_mode": "off"}
        label, settings = ctx.EVENT_RENDERERS["hvac_output_stall"](p, "celsius")
        assert "below" in label and "heating" in label and "3 hours" in label
        assert "17°C" in settings and "20°C" in settings and "°F" not in settings
        label2, _ = ctx.EVENT_RENDERERS["hvac_output_stall_recovered"](
            {"reason": "in_band", "minutes_down": 40}, "fahrenheit"
        )
        assert "no longer" in label2


class TestStatusRefreshOnTransition:
    """hvac_control_issues is computed before the incident scan, so the stall entry would lag a
    whole 30-min update; one refresh is requested on open and on recovery -- never otherwise."""

    def test_refresh_exactly_once_on_open_and_once_on_recovery(self):
        r = _rig()
        try:
            _run_half_hours(r, 2.5, 62.0)
            assert r.refreshes == 0
            r.tick(180, 62.0)  # open
            assert r.refreshes == 1
            for m in range(210, 600, 30):  # steady state: no refresh storm
                r.tick(m, 62.0)
            assert r.refreshes == 1
            r.tick(180 + 12 * 60, 62.0)  # reminder push is not a transition
            assert r.refreshes == 1
            r.tick(900, 70.0)  # recovery
            assert r.refreshes == 2
            r.tick(930, 70.0)
            assert r.refreshes == 2
        finally:
            r.close()

    def test_no_refresh_while_held_by_mode_episode(self):
        r = _rig()
        try:
            r.ae._hvac_write_record = SimpleNamespace(episode_reason="heat", mismatch_since=T0)
            _run_half_hours(r, 5, 62.0)
            assert r.refreshes == 0
        finally:
            r.close()


class TestExclusionDuringAnnouncedStall:
    """An exclusion mid-episode must neither recover nor re-announce (verifier finding 1)."""

    def test_unavailable_thermostat_does_not_recover_or_realert(self):
        r = _rig()
        try:
            _run_half_hours(r, 3, 62.0)  # announced at t=180
            assert r.names() == ["hvac_output_stall"] and len(r.notifications) == 1
            r.set_thermostat(mode="unavailable")
            r.tick(200, 62.0)
            r.tick(230, 62.0)
            r.set_thermostat(mode="off", action="idle")
            for m in range(260, 180 + 12 * 60, 30):  # back, pending clock restarts, 3 h passes
                r.tick(m, 62.0)
            assert r.names() == ["hvac_output_stall"], "no recovery row, no second stall row"
            assert len(r.notifications) == 1, "no second push at +3 h after the exclusion"
            # Reminder cadence is still 12 h from the LAST push (t=180), not restarted.
            r.tick(180 + 12 * 60, 62.0)
            assert len(r.notifications) == 2 and r.names() == ["hvac_output_stall"]
        finally:
            r.close()

    def test_real_recovery_after_an_exclusion_still_reported(self):
        r = _rig()
        try:
            _run_half_hours(r, 3, 62.0)
            r.set_thermostat(mode="unavailable")
            r.tick(200, 62.0)
            r.set_thermostat(mode="off", action="idle")
            r.tick(260, 70.0)
            assert r.names() == ["hvac_output_stall", "hvac_output_stall_recovered"]
            assert r.events[-1][1]["reason"] == "in_band"
        finally:
            r.close()

    def test_equipment_that_ran_during_the_exclusion_is_a_recovery(self):
        r = _rig()
        try:
            _run_half_hours(r, 3, 62.0)
            r.ae._manual_override_active = True
            r.tick(200, 62.0)
            r.coord._note_equipment_activity("heating", now=T0 + timedelta(minutes=210))
            r.ae._manual_override_active = False
            r.tick(240, 62.0)
            assert r.names() == ["hvac_output_stall", "hvac_output_stall_recovered"]
            assert r.events[-1][1]["reason"] == "equipment_ran"
        finally:
            r.close()


class TestAgainstTheGrainDeadband:
    """CA deliberately does not act against the day's grain inside the comfort-family deadband,
    so the stall must not complain about a direction CA itself would not yet act on."""

    def test_warm_house_on_cold_sunny_day_thermostat_in_heat_is_quiet(self):
        """Cold day (heating native, cooling against-grain 5 F): 77 F vs 75 F ceiling is CA's own choice."""
        r = _rig(day_type="cold", mode="heat", action="idle")
        try:
            _run_half_hours(r, 10, 77.0)
            assert r.events == [] and r.notifications == []
        finally:
            r.close()

    def test_cool_house_on_hot_day_is_quiet_inside_the_deadband(self):
        """Hot day (cooling native, heating against-grain 5 F): 66 F vs 68 F floor must not alert."""
        r = _rig(day_type="hot", mode="cool", action="idle")
        try:
            _run_half_hours(r, 10, 66.0)
            assert r.events == [] and r.notifications == []
        finally:
            r.close()

    def test_against_grain_alerts_once_past_deadband_plus_margin(self):
        r = _rig(day_type="hot", mode="off")
        try:
            _run_half_hours(r, 3, 61.0)  # 7 F below floor > 5 + 1
            assert r.names() == ["hvac_output_stall"]
        finally:
            r.close()

    def test_with_the_grain_keeps_the_flat_margin_the_1048_shape(self):
        """Below the floor on a cold day with the thermostat Off: the #1048 case still alerts."""
        r = _rig(day_type="cold", mode="off")
        try:
            _run_half_hours(r, 3, 66.5)  # 1.5 F below floor > 1 F flat margin
            assert r.names() == ["hvac_output_stall"]
        finally:
            r.close()

    def test_mild_day_has_no_native_direction_so_both_directions_use_the_deadband(self):
        r = _rig(day_type="mild", mode="off")
        try:
            _run_half_hours(r, 6, 66.0)  # 2 F below: inside 2 + 1
            assert r.events == []
        finally:
            r.close()
        r = _rig(day_type="mild", mode="off")
        try:
            _run_half_hours(r, 3, 64.0)  # 4 F below: past 2 + 1
            assert r.names() == ["hvac_output_stall"]
        finally:
            r.close()

    def test_stall_margins_table(self):
        assert stall_margins("heating", 5.0, 1.0) == (1.0, 6.0)
        assert stall_margins("cooling", 5.0, 1.0) == (6.0, 1.0)
        assert stall_margins(None, 2.0, 1.0) == (3.0, 3.0)
