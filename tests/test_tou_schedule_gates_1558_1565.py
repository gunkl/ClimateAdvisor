"""TOU schedules: limit to cooling/heating days (#1558) and enable by an entity (#1565).

Occupant view:
- #1558: in a gas-furnace home, a "Cooling only" schedule no longer pre-heats to the comfort ceiling before a weekday
  high-cost window: heating is not electric, so banking heat saved nothing and only moved the thermostat.
- #1565: on a seasonal rate, a schedule enabled by a peak-season helper no longer pre-conditions out of season.
- A helper that is missing or unavailable never silently stops pre-conditioning (fails open, with a warning).
- Every schedule left at its defaults behaves exactly as before.
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import pytest  # noqa: E402

from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402
from custom_components.climate_advisor.entity_health import (  # noqa: E402
    TOU_CONDITION_CONFIG_KEY,
    entity_health_consequence,
    run_entity_health_sweep,
)
from custom_components.climate_advisor.scheduler import (  # noqa: E402
    SKIP_CONDITION_OFF,
    SKIP_NOT_APPLIED_COOL,
    SKIP_NOT_APPLIED_HEAT,
    Schedule,
    TOUPhase,
    filter_schedules,
    schedule_from_dict,
    schedule_skip_sentence,
    schedule_to_dict,
)

_NOW = "custom_components.climate_advisor.coordinator.dt_util.now"
BASE = {"id": "s1", "name": "Weekday peak", "days": ["mon"], "start": "14:00:00", "end": "19:00:00", "cost_tag": "high"}
LEAD = datetime(2026, 8, 31, 13, 40)  # Monday, inside the 45-min default lead before 14:00
IN_WINDOW = datetime(2026, 8, 31, 15, 0)


def _s(**kw) -> Schedule:
    return schedule_from_dict({**BASE, **kw})


# ── Storage: tolerant reader, minimal writer ──────────────────────────────────────────────────────────────────
class TestStorage:
    def test_unknown_keys_are_ignored_not_fatal(self):
        """Before: Schedule(**stored) raised on any unknown key and the whole update cycle failed."""
        s = schedule_from_dict({**BASE, "future_field": 1})
        assert s is not None and s.id == "s1"

    def test_missing_required_field_is_skipped(self):
        assert schedule_from_dict({k: v for k, v in BASE.items() if k != "end"}) is None
        assert schedule_from_dict("junk") is None

    def test_days_list_becomes_a_tuple_and_bad_values_fall_back(self):
        s = schedule_from_dict({**BASE, "applies_to": "sometimes", "condition_entity": ""})
        assert s.days == ("mon",) and s.applies_to == "both" and s.condition_entity is None

    def test_defaults_are_not_stored_so_old_schedules_stay_byte_identical(self):
        assert schedule_to_dict(_s()) == BASE
        assert schedule_to_dict(_s(applies_to="cool", condition_entity="binary_sensor.peak")) == {
            **BASE,
            "applies_to": "cool",
            "condition_entity": "binary_sensor.peak",
        }

    def test_round_trip(self):
        s = _s(applies_to="heat", condition_entity="input_boolean.season")
        assert schedule_from_dict(schedule_to_dict(s)) == s


# ── The one filter ─────────────────────────────────────────────────────────────────────────────────────────────
def _states(**m):
    return lambda e: m.get(e)


class TestFilter:
    def test_defaults_pass_everything_through_unchanged(self):
        schedules = [_s(id=f"s{i}") for i in range(4)]
        for mode in ("heat", "cool", "off", None):
            f = filter_schedules(schedules, _states(), mode)
            assert list(f.kept) == schedules and not f.skipped and not f.failed_open

    @pytest.mark.parametrize(
        ("applies_to", "mode", "kept", "reason"),
        [
            ("cool", "heat", False, SKIP_NOT_APPLIED_HEAT),
            ("cool", "cool", True, None),
            ("heat", "cool", False, SKIP_NOT_APPLIED_COOL),
            ("heat", "heat", True, None),
            ("cool", "off", True, None),  # off day: kept, so "no pre-conditioning needed today" still shows
            ("heat", None, True, None),
        ],
    )
    def test_applies_to(self, applies_to, mode, kept, reason):
        f = filter_schedules([_s(applies_to=applies_to)], _states(), mode)
        assert bool(f.kept) is kept
        assert [r for _s_, r in f.skipped] == ([reason] if reason else [])

    @pytest.mark.parametrize(
        ("state", "kept", "failed_open"),
        [
            ("on", True, False),
            ("off", False, False),
            ("unavailable", True, True),
            ("unknown", True, True),
            (None, True, True),
        ],
    )
    def test_condition_entity(self, state, kept, failed_open):
        f = filter_schedules(
            [_s(condition_entity="binary_sensor.peak")], _states(**{"binary_sensor.peak": state}), "cool"
        )
        assert bool(f.kept) is kept
        assert bool(f.failed_open) is failed_open
        if not kept:
            assert f.skipped[0][1] == SKIP_CONDITION_OFF

    def test_applies_to_is_checked_before_the_entity(self):
        """A heat-only schedule on a cool day is skipped without reading (or warning about) its broken helper."""
        f = filter_schedules([_s(applies_to="heat", condition_entity="binary_sensor.gone")], _states(), "cool")
        assert f.failed_open == () and f.skipped[0][1] == SKIP_NOT_APPLIED_COOL

    def test_order_is_preserved_so_the_earliest_schedule_still_wins(self):
        a, b, c = _s(id="a"), _s(id="b", applies_to="heat"), _s(id="c")
        assert [s.id for s in filter_schedules([a, b, c], _states(), "cool").kept] == ["a", "c"]

    def test_one_wording(self):
        assert schedule_skip_sentence(SKIP_NOT_APPLIED_HEAT) == "not applied to heating"
        assert schedule_skip_sentence(SKIP_NOT_APPLIED_COOL) == "not applied to cooling"
        assert schedule_skip_sentence(SKIP_CONDITION_OFF) == "its condition is off"


# ── Coordinator: the real resolve chain, in the lead window and in the window ─────────────────────────────────
def _classification(mode: str) -> DayClassification:
    c = object.__new__(DayClassification)
    c.__dict__.update(
        day_type="hot" if mode == "cool" else "cold",
        trend_direction="stable",
        trend_magnitude=0,
        today_high=90,
        today_low=60,
        tomorrow_high=90,
        tomorrow_low=60,
        hvac_mode=mode,
        pre_condition=False,
        pre_condition_target=None,
        windows_recommended=False,
        window_open_time=None,
        window_close_time=None,
        setback_modifier=0.0,
        window_opportunity_morning=False,
        window_opportunity_evening=False,
    )
    return c


def _coord(mode: str, schedule: dict, states: dict | None = None) -> ClimateAdvisorCoordinator:
    c = object.__new__(ClimateAdvisorCoordinator)
    c.config = {"schedules": [schedule], "comfort_heat": 68.0, "comfort_cool": 75.0, "temp_unit": "fahrenheit"}
    c._current_classification = _classification(mode)
    c._event_log = []
    c._tou_phase_resolution = None
    c._tou_active_cost_resolution = None
    c._tou_active_window_notified = False
    c._occupancy_mode = "home"
    c._automation_enabled = True
    c._startup_coalesce_active = False
    c._get_indoor_temp = MagicMock(return_value=74.0 if mode == "cool" else 69.0)
    c._last_outdoor_temp = 90.0 if mode == "cool" else 30.0
    c.hass = MagicMock()
    st = states or {}
    c.hass.states.get.side_effect = lambda e: types.SimpleNamespace(state=st[e]) if e in st else None
    c.automation_engine = MagicMock()
    c.automation_engine._grace_active = False
    c.automation_engine._manual_override_active = False
    c.automation_engine._override_confirm_pending = False
    c.automation_engine.is_paused_by_door = False
    c.automation_engine.natural_vent_active = False
    c.automation_engine._is_within_planned_window_period = MagicMock(return_value=False)
    c._any_sensor_open = MagicMock(return_value=False)
    c.control_thermal_model = lambda m: None
    c.control_house_stepper = lambda: None
    c._thermal_model = None
    c._sync_tou_away_vacation_state = MagicMock()
    c._outdoor_forecast_at = lambda when: c._last_outdoor_temp
    for name in (
        "_resolve_tou_schedule_state",
        "_maybe_emit_tou_active_window_event",
        "_emit_event",
        "_compute_automation_status",
        "_stored_schedules",
        "_tou_condition_state",
        "_log_tou_schedule_filter",
        "_tou_skip_text",
        "_format_tou_ends",
    ):
        setattr(c, name, types.MethodType(getattr(ClimateAdvisorCoordinator, name), c))
    return c


def _resolve(c, now):
    with patch(_NOW, return_value=now):
        c._resolve_tou_schedule_state()
        return c._compute_automation_status()


class TestCoordinatorChain:
    def test_gas_furnace_home_heat_day_cool_only_schedule_does_not_pre_heat(self):
        c = _coord("heat", {**BASE, "applies_to": "cool"})
        _resolve(c, LEAD)
        assert c._tou_phase_resolution.phase is TOUPhase.NONE  # nothing banks: _apply_tou_schedule writes nothing

    def test_twin_the_same_schedule_on_a_cool_day_still_pre_cools(self):
        c = _coord("cool", {**BASE, "applies_to": "cool"})
        _resolve(c, LEAD)
        assert c._tou_phase_resolution.phase is TOUPhase.PRECONDITIONING
        assert c._tou_phase_resolution.mode == "cool" and c._tou_phase_resolution.target == 68.0

    def test_heat_only_mirror(self):
        heat = _coord("heat", {**BASE, "applies_to": "heat"})
        _resolve(heat, LEAD)
        cool = _coord("cool", {**BASE, "applies_to": "heat"})
        _resolve(cool, LEAD)
        assert heat._tou_phase_resolution.phase is TOUPhase.PRECONDITIONING
        assert heat._tou_phase_resolution.target == 75.0
        assert cool._tou_phase_resolution.phase is TOUPhase.NONE

    def test_default_schedule_is_unchanged(self):
        c = _coord("heat", BASE)
        _resolve(c, LEAD)
        assert c._tou_phase_resolution.phase is TOUPhase.PRECONDITIONING

    @pytest.mark.parametrize(("state", "banks"), [("on", True), ("off", False), ("unavailable", True), (None, True)])
    def test_enable_entity_in_the_lead_window(self, state, banks):
        states = {} if state is None else {"binary_sensor.peak": state}
        c = _coord("cool", {**BASE, "condition_entity": "binary_sensor.peak"}, states)
        _resolve(c, LEAD)
        assert (c._tou_phase_resolution.phase is TOUPhase.PRECONDITIONING) is banks

    def test_skipped_window_is_shown_with_its_reason_and_recorded(self):
        c = _coord("heat", {**BASE, "applies_to": "cool"})
        status = _resolve(c, IN_WINDOW)
        assert "TOU high-cost period skipped" in status and "not applied to heating" in status
        (event,) = [e for e in c._event_log if e.get("type") == "tou_schedule_window_active"]
        assert event["preconditioned"] is False
        assert event["skipped"] == [{"schedule_id": "s1", "reason": SKIP_NOT_APPLIED_HEAT, "entity": None}]

    def test_window_that_starts_applying_mid_window_is_recorded_again(self):
        states = {"binary_sensor.peak": "off"}
        c = _coord("cool", {**BASE, "condition_entity": "binary_sensor.peak"}, states)
        c.hass.states.get.side_effect = lambda e: types.SimpleNamespace(state=states[e]) if e in states else None
        _resolve(c, IN_WINDOW)
        states["binary_sensor.peak"] = "on"
        _resolve(c, IN_WINDOW)
        events = [e for e in c._event_log if e.get("type") == "tou_schedule_window_active"]
        assert [("skipped" in e) for e in events] == [True, False]

    def test_off_day_keeps_the_existing_status_line(self):
        c = _coord("off", {**BASE, "applies_to": "cool"})
        assert "no pre-conditioning needed today" in _resolve(c, IN_WINDOW)

    def test_an_unreadable_stored_record_no_longer_fails_the_cycle(self):
        c = _coord("cool", {**BASE, "extra": 1})  # a field this build does not know
        c.config["schedules"].append({"id": "broken"})
        _resolve(c, LEAD)  # must not raise
        assert c._tou_phase_resolution.phase is TOUPhase.PRECONDITIONING

    def test_failed_open_warns_once(self, caplog):
        c = _coord("cool", {**BASE, "condition_entity": "binary_sensor.gone"})
        with caplog.at_level("WARNING"):
            _resolve(c, LEAD)
            _resolve(c, LEAD)
        assert sum("condition unreadable" in r.message for r in caplog.records) == 1


# ── The enable-entity listener: acts within seconds, not at the next 30-minute cycle ───────────────────────────
def _listener_coord(states: dict) -> ClimateAdvisorCoordinator:
    c = _coord("cool", {**BASE, "condition_entity": "binary_sensor.peak"}, states)
    c.hass.states.get.side_effect = lambda e: types.SimpleNamespace(state=states[e]) if e in states else None
    c._apply_tou_schedule = AsyncMock()
    c._reapply_classification_after_tou_change = AsyncMock()
    c._push_tou_bank_card_status = MagicMock()
    c._suppress_during_startup_coalescing = MagicMock(return_value=False)
    for name in ("_async_tou_condition_changed", "_tou_bank_in_force"):
        setattr(c, name, types.MethodType(getattr(ClimateAdvisorCoordinator, name), c))
    c.automation_engine.tou_bank_gates_pass = MagicMock(return_value=True)
    return c


def _event(old, new):
    return types.SimpleNamespace(
        data={
            "entity_id": "binary_sensor.peak",
            "old_state": types.SimpleNamespace(state=old),
            "new_state": types.SimpleNamespace(state=new),
        }
    )


def _fire(c, old, new, now):
    with patch(_NOW, return_value=now):
        asyncio.run(c._async_tou_condition_changed(_event(old, new)))


class TestListener:
    def test_turning_on_in_the_lead_window_starts_the_bank_now(self):
        states = {"binary_sensor.peak": "off"}
        c = _listener_coord(states)
        _resolve(c, LEAD)
        assert c._tou_phase_resolution.phase is TOUPhase.NONE
        states["binary_sensor.peak"] = "on"
        _fire(c, "off", "on", LEAD)
        c._apply_tou_schedule.assert_awaited_once()
        c._reapply_classification_after_tou_change.assert_not_awaited()

    def test_turning_off_mid_bank_writes_the_band_now(self):
        states = {"binary_sensor.peak": "on"}
        c = _listener_coord(states)
        _resolve(c, LEAD)
        assert c._tou_phase_resolution.phase is TOUPhase.PRECONDITIONING
        states["binary_sensor.peak"] = "off"
        _fire(c, "on", "off", LEAD)
        c._reapply_classification_after_tou_change.assert_awaited_once()
        c._apply_tou_schedule.assert_not_awaited()

    def test_an_attribute_only_change_does_nothing(self):
        c = _listener_coord({"binary_sensor.peak": "on"})
        _resolve(c, LEAD)
        _fire(c, "on", "on", LEAD)
        c._apply_tou_schedule.assert_not_awaited()
        c._reapply_classification_after_tou_change.assert_not_awaited()

    def test_turning_off_outside_any_bank_writes_nothing(self):
        states = {"binary_sensor.peak": "on"}
        c = _listener_coord(states)
        _resolve(c, datetime(2026, 8, 31, 9, 0))  # no bank or window in force
        states["binary_sensor.peak"] = "off"
        _fire(c, "on", "off", datetime(2026, 8, 31, 9, 0))
        c._apply_tou_schedule.assert_not_awaited()
        c._reapply_classification_after_tou_change.assert_not_awaited()

    def _away_window(self, c, *, expansion=1.5):
        return types.SimpleNamespace(
            phase=TOUPhase.NONE,
            expansion_f=expansion,
            schedule_start=datetime(2026, 8, 31, 14, 0),
            window_end=datetime(2026, 8, 31, 19, 0),
            schedule_id="s1",
            target=None,
            mode="cool",
            precondition_start=None,
        )

    def test_away_widening_ending_mid_window_writes_the_band(self):
        c = _listener_coord({"binary_sensor.peak": "off"})
        c._occupancy_mode = "away"
        c._tou_phase_resolution = self._away_window(c)

        def _resolve_skipping_s1():
            c._tou_phase_resolution = None
            c._tou_schedule_filter = filter_schedules([_s(condition_entity="binary_sensor.peak")], _states(), "cool")
            c._tou_schedule_filter = type(c._tou_schedule_filter)((), ((_s(), SKIP_CONDITION_OFF),), ())

        c._resolve_tou_schedule_state = MagicMock(side_effect=_resolve_skipping_s1)
        _fire(c, "on", "off", IN_WINDOW)
        c._reapply_classification_after_tou_change.assert_awaited_once()

    def test_away_widening_starts_when_the_entity_turns_on_mid_window(self):
        """Review mirror: the entity turning ON during an Away/Vacation high-cost period widens the band at once."""
        c = _listener_coord({"binary_sensor.peak": "on"})
        c._occupancy_mode = "away"
        c._tou_phase_resolution = None

        def _resolve_applying():
            c._tou_phase_resolution = self._away_window(c)
            c._tou_schedule_filter = filter_schedules([_s()], _states(), "cool")

        c._resolve_tou_schedule_state = MagicMock(side_effect=_resolve_applying)
        _fire(c, "off", "on", IN_WINDOW)
        c._reapply_classification_after_tou_change.assert_awaited_once()

    def test_another_schedules_entity_never_ends_a_healthy_bank(self):
        """Review: schedule A banks; schedule B's helper turns off. A's bank is left alone."""
        states = {"binary_sensor.peak": "on", "binary_sensor.other": "on"}
        c = _listener_coord(states)
        c.config["schedules"] = [
            {**BASE, "condition_entity": "binary_sensor.peak"},
            {**BASE, "id": "s2", "start": "20:00:00", "end": "21:00:00", "condition_entity": "binary_sensor.other"},
        ]
        _resolve(c, LEAD)
        assert c._tou_phase_resolution.schedule_id == "s1"
        c._thermostat_holds_tou_bank = MagicMock(return_value=True)
        states["binary_sensor.other"] = "off"
        _fire(c, "on", "off", LEAD)
        c._apply_tou_schedule.assert_not_awaited()  # the same bank, already held: never rewritten
        c._reapply_classification_after_tou_change.assert_not_awaited()

    def test_a_gap_resolve_does_not_end_the_bank(self):
        """Review: a re-resolve that lands on phase NONE while the banking schedule still applies writes nothing."""
        c = _listener_coord({"binary_sensor.peak": "on"})
        _resolve(c, LEAD)
        c._resolve_tou_schedule_state = MagicMock(
            side_effect=lambda: setattr(c, "_tou_phase_resolution", types.SimpleNamespace(phase=TOUPhase.NONE))
        )
        _fire(c, "on", "unavailable", LEAD)
        c._reapply_classification_after_tou_change.assert_not_awaited()


# ── Entity health ──────────────────────────────────────────────────────────────────────────────────────────────
def test_each_enable_entity_gets_its_own_health_slot_and_says_the_schedule_still_applies():
    hass = MagicMock()
    hass.states.get.side_effect = lambda e: None
    config = {
        "schedules": [
            {**BASE, "condition_entity": "binary_sensor.a"},
            {**BASE, "id": "s2", "condition_entity": "binary_sensor.b"},
            {**BASE, "id": "s3", "condition_entity": "binary_sensor.a"},  # shared helper counted once
        ]
    }
    issues = [i for i in run_entity_health_sweep(hass, config) if i.config_key == TOU_CONDITION_CONFIG_KEY]
    assert [i.entity_id for i in issues] == ["binary_sensor.a", "binary_sensor.b"]
    assert len({i.state_key for i in issues}) == 2
    assert "still applies" in entity_health_consequence(issues[0])


def test_every_scenario_schedule_uses_only_known_fields():
    """The reader ignores unknown schedule keys (forward safety), so a typo in a scenario ("applies_too") would pass
    silently while testing nothing. Every committed scenario's schedules must use only known fields."""
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from custom_components.climate_advisor.scheduler import SCHEDULE_FIELDS  # noqa: PLC0415

    root = Path(__file__).resolve().parent.parent / "tools" / "simulations"
    bad = []
    seen = 0
    for path in root.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        config = data.get("config") if isinstance(data, dict) else None
        for schedule in (config or {}).get("schedules") or []:
            seen += 1
            unknown = set(schedule) - set(SCHEDULE_FIELDS)
            if unknown:
                bad.append((path.name, sorted(unknown)))
    assert seen > 20
    assert bad == []


def test_summary_survives_an_unknown_applies_to_value():
    from custom_components.climate_advisor.config_flow import ClimateAdvisorOptionsFlow

    label = ClimateAdvisorOptionsFlow._format_schedule_summary({**BASE, "applies_to": "sometimes"})
    assert "Weekday peak" in label and "only" not in label


def test_skip_text_joins_reasons_once_and_names_the_entity():
    from custom_components.climate_advisor.scheduler import schedule_skip_text

    assert schedule_skip_text([(SKIP_CONDITION_OFF, "input_boolean.x"), (SKIP_CONDITION_OFF, "input_boolean.x")]) == (
        "its condition is off (input_boolean.x)"
    )
    assert schedule_skip_text([(SKIP_NOT_APPLIED_HEAT, None)]) == "not applied to heating"


def test_a_low_tag_schedule_starting_mid_period_is_not_a_new_period():
    low = {**BASE, "id": "low", "start": "16:00:00", "end": "17:00:00", "cost_tag": "low"}
    c = _coord("cool", BASE)
    c.config["schedules"].append(low)
    _resolve(c, IN_WINDOW)
    _resolve(c, datetime(2026, 8, 31, 16, 30))
    assert len([e for e in c._event_log if e.get("type") == "tou_schedule_window_active"]) == 1
