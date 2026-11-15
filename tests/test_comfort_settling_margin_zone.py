"""Issue #1073 (reviewer B1/M1/M2 + minors): the settling guard covers the MARGIN zone; fixed mode-set labels;
CA reversing an occupant's mode change is recorded; stale holds are released; Debug label is absolute and unit-aware.

Occupant framing: after a cool bank, a pre-cool or the occupant's own cooling the house usually reads 68-70 FIRST
(inside the 2 F heat arming margin, not yet under the floor). Heat used to arm there with no guard, and a later
overshoot to 67 then ran the furnace minutes after the AC. The guard now holds an AGAINST-THE-GRAIN switch away
from a family that just ran, in the margin zone and in the breach zone up to the tolerance; deeper than the
tolerance past the edge always switches at once (R1). Nothing here re-implements production logic: the real
engine, the real selector and the real leaf are driven.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.climate_advisor.comfort_family_decision import (
    ComfortFamilyInputs,
    ComfortFamilyOutcome,
    decide_comfort_family,
)
from tests.test_comfort_settling_guard import (
    _AUTOMATION,
    _T0,
    TestR2,
    _apply,
    _classification,
    _Clock,
    _coordinator_class,
    _make_engine,
    _mode,
    _mode_writes,
    _reactive,
    _run,
    _scene,
    _set_indoor,
)


def _events(engine, name):
    return [p for n, p in getattr(engine, "events", []) if n == name]


def _capture_events(engine):
    engine.events = []
    engine._emit_event_callback = lambda name, payload: engine.events.append((name, payload))


# ---------------------------------------------------------------------------
# Leaf: zone + non-negative depth
# ---------------------------------------------------------------------------


def _leaf(**kw):
    base = {
        "current_family": "cooling",
        "default_family": "cooling",
        "indoor": 69.0,
        "floor": 68.0,
        "ceiling": 74.0,
        "margin_f": 2.0,
        "natural_vent_active": False,
        "whf_owns_hvac": False,
        "now": _T0,
        "tolerance_f": 2.0,
        "native_family": "cooling",
        "opposite_ran_min_ago": 16.0,
        "recency_window_min": 120.0,
    }
    base.update(kw)
    return decide_comfort_family(ComfortFamilyInputs(**base))


class TestLeafZones:
    def test_margin_zone_hold_reports_zone_and_zero_depth(self):
        d = _leaf(indoor=69.0)
        assert d.outcome is ComfortFamilyOutcome.HOLD and d.held
        assert d.hold_zone == "margin" and d.hold_depth_f == 0.0 and d.breach is False
        assert d.target_family == "cooling" and d.hold_candidate_family == "heating"

    def test_breach_zone_hold_reports_positive_depth(self):
        d = _leaf(indoor=67.0)
        assert d.held and d.hold_zone == "breach" and d.hold_depth_f == pytest.approx(1.0) and d.breach is True

    def test_deeper_than_tolerance_switches(self):
        d = _leaf(indoor=65.9)
        assert d.outcome is ComfortFamilyOutcome.SWITCH and not d.held

    def test_native_margin_switch_is_immediate(self):
        d = _leaf(indoor=69.0, native_family="heating")
        assert d.outcome is ComfortFamilyOutcome.SWITCH

    def test_cool_mirror_margin_zone(self):
        d = _leaf(current_family="heating", indoor=73.0, native_family="heating")
        assert d.held and d.hold_zone == "margin" and d.hold_candidate_family == "cooling"

    def test_hysteresis_gap_is_unchanged(self):
        d = _leaf(indoor=71.0)
        assert d.outcome is ComfortFamilyOutcome.HOLD and not d.held and d.target_family == "cooling"

    def test_unknown_current_family_adopts_in_the_margin_zone(self):
        d = _leaf(current_family=None, indoor=69.0, native_family=None)
        assert not d.held


# ---------------------------------------------------------------------------
# B1 reproductions through the real engine
# ---------------------------------------------------------------------------


class TestReviewerReproductions:
    def test_hot_day_ac_ran_16_min_ago_reading_69_then_67_stays_in_cool(self):
        engine, clock = _scene(day="hot", mode="cool", indoor=69.0, ran="cool", ran_min=16.0)
        assert _mode(engine) == "cool", "69 is inside the heat margin: previously armed heat"
        assert engine._last_comfort_hold["zone"] == "margin"
        clock.advance(minutes=5)
        _apply(engine, clock, "hot", 67.0)
        assert _mode(engine) == "cool"
        assert engine._last_comfort_hold["zone"] == "breach"
        assert _mode_writes(engine).count("heat") == 0

    def test_cold_day_furnace_ran_then_73_then_75_stays_in_heat(self):
        engine, clock = _scene(day="cold", mode="heat", indoor=73.0, ran="heat", ran_min=16.0)
        assert _mode(engine) == "heat"
        assert engine._last_comfort_hold["zone"] == "margin"
        clock.advance(minutes=5)
        _apply(engine, clock, "cold", 75.0)
        assert _mode(engine) == "heat"
        assert _mode_writes(engine).count("cool") == 0

    @pytest.mark.parametrize("day", ["hot", "warm", "mild"])
    def test_heat_does_not_arm_in_the_margin_after_cool_ran(self, day):
        engine, _ = _scene(day=day, mode="cool", indoor=68.5, ran="cool", ran_min=10.0)
        assert _mode(engine) == "cool"

    def test_deeper_than_tolerance_still_switches_at_once(self):
        engine, clock = _scene(day="hot", mode="cool", indoor=69.0, ran="cool", ran_min=16.0)
        _reactive(engine, clock, 62.9)  # depth 5.1 > 5
        assert _mode(engine) == "heat"


class TestTouBankEndMargin:
    @pytest.mark.parametrize("occupancy", ["home", "guest", "away", "vacation"])
    @pytest.mark.parametrize("direction", ["cool", "heat"])
    def test_first_reading_inside_the_margin_then_overshoot_stays_in_the_bank_mode(self, direction, occupancy):
        engine, clock, _c = TestR2._bank(direction, occupancy)
        band = engine._last_comfort_band
        day = "hot" if direction == "cool" else "cold"
        # the first reading after the bank is INSIDE the margin of the edge the bank pushed toward
        first = band.floor + 1.0 if direction == "cool" else band.ceiling - 1.0
        later = band.floor - 1.0 if direction == "cool" else band.ceiling + 1.0
        _apply(engine, clock, day, first)
        assert _mode(engine) == direction, f"{occupancy}: margin reading must not flip the thermostat"
        clock.advance(minutes=3)
        _apply(engine, clock, day, later)
        assert _mode(engine) == direction, f"{occupancy}: later overshoot must still be held"
        assert engine._last_comfort_hold is not None


class TestMarginZoneBoundaries:
    def test_margin_switch_proceeds_after_the_window_lapses(self):
        engine, _ = _scene(day="mild", mode="cool", indoor=69.5, ran="cool", ran_min=130.0)
        assert _mode(engine) == "heat"

    def test_margin_switch_proceeds_after_the_window_lapses_mirror(self):
        engine, _ = _scene(day="mild", mode="heat", indoor=72.5, ran="heat", ran_min=130.0)
        assert _mode(engine) == "cool"

    @pytest.mark.parametrize(
        ("day", "mode", "indoor", "ran", "expected"),
        [("cold", "cool", 69.5, "cool", "heat"), ("hot", "heat", 72.5, "heat", "cool")],
    )
    def test_native_margin_switch_is_immediate(self, day, mode, indoor, ran, expected):
        engine, _ = _scene(day=day, mode=mode, indoor=indoor, ran=ran, ran_min=5.0)
        assert _mode(engine) == expected
        assert engine._last_comfort_hold is None

    def test_mild_holds_on_both_sides_in_the_margin(self):
        e1, _ = _scene(day="mild", mode="cool", indoor=69.5, ran="cool", ran_min=10.0)
        e2, _ = _scene(day="mild", mode="heat", indoor=72.5, ran="heat", ran_min=10.0)
        assert _mode(e1) == "cool" and _mode(e2) == "heat"

    @pytest.mark.parametrize("day", ["mild", "cold"])
    @pytest.mark.parametrize("indoor", [66.0, 67.0, 68.0, 69.0])
    def test_1066_cold_morning_default_cool_command_no_run_heat_arms_at_the_first_reading(self, day, indoor):
        """The #1066 regression: the default cool command is a selector write (not deliberate) and no equipment
        ran, so a cold-morning house at 66-69 must arm heat at the very first reading."""
        engine = _make_engine(indoor=indoor, mode="off")
        clock = _Clock()
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), engine._mode_change_context("selector"):
            _run(engine._set_hvac_mode("cool", reason="default cool command"))
        assert _mode(engine) == "cool"
        clock.advance(minutes=11)  # past the 600 s dwell lock (a margin-zone switch is not a breach)
        _apply(engine, clock, day, indoor)
        assert _mode(engine) == "heat"
        assert engine._last_comfort_hold is None


# ---------------------------------------------------------------------------
# Minor 1: a stale hold is released when the selector is skipped
# ---------------------------------------------------------------------------


class TestStaleHoldReleased:
    def _held(self):
        engine, clock = _scene(day="hot", mode="cool", indoor=67.0, ran="cool", ran_min=10.0)
        assert engine._last_comfort_hold is not None
        assert engine.comfort_holds[-1]["end_ts"] is None
        return engine, clock

    def _assert_released(self, engine):
        assert engine._last_comfort_hold is None
        assert engine.comfort_holds[-1]["end_reason"] == "lapsed"
        assert engine.comfort_holds[-1]["end_ts"] is not None

    def test_tou_precondition_window(self):
        engine, clock = self._held()
        engine.set_tou_precondition_window(clock.now + timedelta(hours=1), "cool")
        _apply(engine, clock, "hot", 67.0)
        self._assert_released(engine)

    def test_tou_precondition_window_reactive_tick(self):
        engine, clock = self._held()
        engine.set_tou_precondition_window(clock.now + timedelta(hours=1), "cool")
        _reactive(engine, clock, 67.0)
        self._assert_released(engine)

    def test_override_confirmation_pending(self):
        engine, clock = self._held()
        engine._override_confirm_pending = True
        _apply(engine, clock, "hot", 67.0)
        self._assert_released(engine)

    def test_manual_override_active_reactive_tick(self):
        engine, clock = self._held()
        engine._manual_override_active = True
        _reactive(engine, clock, 67.0)
        self._assert_released(engine)

    def test_nat_vent_owns_hvac(self):
        engine, clock = self._held()
        engine._natural_vent_active = True
        _reactive(engine, clock, 67.0)
        self._assert_released(engine)

    def test_door_window_pause(self):
        engine, clock = self._held()
        engine._paused_by_door = True
        with patch.object(type(engine), "_sync_paused_by_door_with_live_sensors", new=AsyncMock(return_value=None)):
            _apply(engine, clock, "hot", 67.0)
        self._assert_released(engine)


# ---------------------------------------------------------------------------
# Minor 2: Debug label (absolute until, user's unit)
# ---------------------------------------------------------------------------


class TestDebugLabel:
    def _label(self, hold, unit="fahrenheit"):
        engine = _make_engine(indoor=67.0, mode="cool")
        engine._last_comfort_hold = hold
        return _coordinator_class()._comfort_family_hold_label(engine, _T0, unit)

    def test_until_is_the_absolute_stored_time_and_stable_between_evaluations(self):
        engine, clock = _scene(day="hot", mode="cool", indoor=67.0, ran="cool", ran_min=40.0)
        until1 = engine._last_comfort_hold["until_ts"]
        clock.advance(minutes=10)
        _apply(engine, clock, "hot", 67.0)
        assert engine._last_comfort_hold["until_ts"] == until1, "ran timestamp + window does not drift"
        # ran 40 min before t0 + window 120 = t0 + 80 min
        assert until1 == (_T0 + timedelta(minutes=80)).isoformat()
        text = _coordinator_class()._comfort_family_hold_label(engine, clock.now, "fahrenheit")
        assert "(until ~3:20 PM)" in text  # Issue #1341: the one 12-hour formatter, i18n.format_time

    def test_celsius_user_sees_no_fahrenheit_numbers(self):
        hold = {
            "candidate_family": "heating",
            "zone": "breach",
            "depth_f": 1.8,
            "tolerance_f": 5.0,
            "opposite_ran_min_ago": 12.0,
            "window_min": 120.0,
            "until_ts": (_T0 + timedelta(minutes=108)).isoformat(),
        }
        text = self._label(hold, "celsius")
        assert "°C" in text and "°F" not in text and " F " not in text
        assert "1.0°C past floor" in text and "tolerance 2.8°C" in text
        assert "(until ~3:48 PM)" in text

    def test_margin_zone_text(self):
        hold = {
            "candidate_family": "heating",
            "zone": "margin",
            "depth_f": 0.0,
            "tolerance_f": 2.0,
            "opposite_ran_min_ago": 10.0,
            "window_min": 120.0,
            "until_ts": (_T0 + timedelta(minutes=110)).isoformat(),
        }
        assert "near the floor, still inside the comfort range" in self._label(hold)


# ---------------------------------------------------------------------------
# M1: fixed labels, no entity id / schedule id in the event
# ---------------------------------------------------------------------------


class TestModeSetReasonIsFixed:
    def test_pause_reason_with_entity_id_and_tou_schedule_id_never_reach_the_event(self):
        engine = _make_engine(indoor=71.0, mode="off")
        _capture_events(engine)
        clock = _Clock()
        engine._last_commanded_hvac_mode = "off"
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            with engine._mode_change_context("pause"):
                _run(engine._set_hvac_mode("heat", reason="door/window binary_sensor.front_door_secret opened"))
            clock.advance(minutes=5)
            c = _classification("hot")
            engine._current_classification = c
            _run(engine.apply_tou_precondition(c, 64.0, "schedule=SCHED_SECRET_42"))
        rows = _events(engine, "thermostat_mode_set")
        assert {r["source"] for r in rows} >= {"pause", "tou"}
        blob = json.dumps(rows)
        assert "front_door_secret" not in blob and "binary_sensor" not in blob
        assert "SCHED_SECRET_42" not in blob
        by_source = {r["source"]: r["reason"] for r in rows}
        assert by_source["pause"] == "door or window opened"
        assert by_source["tou"] == "time-of-use schedule"

    def test_every_ring_reason_has_a_fixed_label(self):
        from custom_components.climate_advisor.automation import _MODE_CHANGE_REASONS, _MODE_SET_REASON_LABELS

        assert set(_MODE_SET_REASON_LABELS) == set(_MODE_CHANGE_REASONS)

    def test_hold_and_switch_payloads_carry_no_entity_id(self):
        engine, clock = _scene(day="hot", mode="cool", indoor=67.0, ran="cool", ran_min=10.0)
        _capture_events(engine)
        clock.advance(minutes=30)
        _apply(engine, clock, "hot", 60.0)  # switch
        assert _events(engine, "comfort_family_switch")
        assert "climate.thermostat" not in json.dumps(engine.events)


# ---------------------------------------------------------------------------
# M2: a CA write that reverses an occupant's mode change is recorded
# ---------------------------------------------------------------------------


class TestReversalOfUserModeIsRecorded:
    def _scene(self):
        engine = _make_engine(indoor=71.0, mode="off")
        _capture_events(engine)
        clock = _Clock()
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            with engine._mode_change_context("selector"):
                _run(engine._set_hvac_mode("cool", reason="selector cool"))
            assert _mode(engine) == "cool"
            clock.advance(minutes=10)
            engine._test_state.state = "heat"  # the occupant switched to heat at the thermostat
            engine._append_mode_change("cool", "heat", "user")
        return engine, clock

    def test_ca_cool_after_the_user_heat_is_recorded(self):
        engine, clock = self._scene()
        engine._last_resolved_family = "cooling"
        n_before = len(engine.comfort_mode_changes)
        clock.advance(minutes=31)  # grace ended
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), engine._mode_change_context("restore"):
            _run(engine._set_hvac_mode("cool", reason="grace ended, restoring cool"))
        changes = engine.comfort_mode_changes
        assert len(changes) == n_before + 1
        assert changes[-1]["to_mode"] == "cool" and changes[-1]["from_mode"] == "heat"
        rows = _events(engine, "thermostat_mode_set")
        assert rows[-1]["to_mode"] == "cool" and rows[-1]["from_mode"] == "heat"
        assert engine._last_resolved_family is None, "a non-selector change clears the remembered wish"

    def test_a_reaffirm_still_records_nothing(self):
        engine, clock = self._scene()
        clock.advance(minutes=31)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), engine._mode_change_context("restore"):
            _run(engine._set_hvac_mode("cool", reason="restore"))  # reverses the user: recorded
        n = len(engine.comfort_mode_changes)
        clock.advance(minutes=5)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), engine._mode_change_context("restore"):
            _run(engine._set_hvac_mode("cool", reason="re-assert"))  # live is already cool
        assert len(engine.comfort_mode_changes) == n

    def test_set_temperature_path_records_once(self):
        engine, clock = self._scene()
        n_before = len(engine.comfort_mode_changes)
        clock.advance(minutes=31)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), engine._mode_change_context("restore"):
            _run(engine._set_temperature(74.0, reason="grace ended", mode="cool"))
        assert _mode(engine) == "cool"
        assert len(engine.comfort_mode_changes) == n_before + 1, (
            "_set_temperature -> _ensure_hvac_mode_for_write must not double-record"
        )

    def test_unreadable_live_state_falls_back_to_the_last_command(self):
        engine = _make_engine(indoor=71.0, mode="cool")
        engine._last_commanded_hvac_mode = "cool"
        clock = _Clock()
        with (
            patch.object(type(engine), "_live_hvac_state", return_value=None),
            patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock),
            engine._mode_change_context("restore"),
        ):
            _run(engine._set_hvac_mode("cool", reason="x"))
        assert engine.comfort_mode_changes == []


# ---------------------------------------------------------------------------
# Docs-claim tests (items 7 and 8): the event-grain "ran" stamp also feeds the #835 HVAC-fan restriction, and
# override adoption compares against the selector's family
# ---------------------------------------------------------------------------


class TestDocumentedSideEffects:
    def test_event_grain_ran_stamp_feeds_the_hvac_fan_restriction(self):
        from tests.test_comfort_settling_guard import _stamp_ran

        engine = _make_engine(indoor=71.0, mode="off", hvac_fan_restrict_mode="heat")
        clock = _Clock()
        _stamp_ran(engine, "cool", 10.0)  # real coordinator writer, event grain
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            reason = engine._hvac_fan_restriction_block_reason()
        assert reason is not None and "cooled within the last 2h" in reason
        engine2 = _make_engine(indoor=71.0, mode="off", hvac_fan_restrict_mode="heat")
        _stamp_ran(engine2, "cool", 180.0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            assert engine2._hvac_fan_restriction_block_reason() is None

    def test_heat_override_on_a_cold_day_near_the_ceiling_is_no_longer_adopted(self):
        """Adoption compares to the SELECTOR family (cool at 73.5, inside the cool margin), not to the cold day's
        classification mode "heat"; the occupant's heat override stays an override."""
        engine = _make_engine(indoor=73.5, mode="heat")
        clock = _Clock()
        c = _classification("cold")  # classification.hvac_mode == "heat"
        engine._current_classification = c
        engine._manual_override_active = True
        engine._manual_override_mode = "heat"
        engine._manual_override_source = "normal"
        engine._test_state.attributes["temperature"] = 68.0
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            assert engine._override_matches_current_decision(c) is False
            # control: with the house mid-band the selector keeps heat and the matching override is adopted
            _set_indoor(engine, 70.0)
            assert engine._override_matches_current_decision(c) is True


# ---------------------------------------------------------------------------
# Round 2: M-A (unavailable live state), M-B (reactive tick records holds), minors
# ---------------------------------------------------------------------------


def _unavailable_engine(commanded="cool"):
    engine = _make_engine(indoor=71.0, mode="unavailable")
    _capture_events(engine)
    engine.hass.services.async_call = AsyncMock(return_value=None)  # an unreachable thermostat never follows
    engine._last_commanded_hvac_mode = commanded
    return engine


class TestUnavailableThermostatRecordsNoFakeChanges:
    def test_live_state_filter_is_shared(self):
        from custom_components.climate_advisor.automation import _real_hvac_mode

        for bad in ("unavailable", "unknown", "", None, 5):
            assert _real_hvac_mode(bad) is None
        for good in ("off", "heat", "cool", "heat_cool", "auto"):
            assert _real_hvac_mode(good) == good
        engine = _make_engine(indoor=70.0, mode="unavailable")
        assert engine._live_hvac_state() is None

    def test_three_cycles_while_unavailable_produce_no_ring_entries_or_rows(self):
        engine = _unavailable_engine("cool")
        clock = _Clock()
        for _ in range(3):
            _apply(engine, clock, "hot", 71.0)
            clock.advance(minutes=30)
        assert engine.comfort_mode_changes == []
        assert _events(engine, "thermostat_mode_set") == []

    def test_tou_write_while_unavailable_does_not_restart_the_window(self):
        engine = _unavailable_engine("cool")
        clock = _Clock()
        c = _classification("hot")
        engine._current_classification = c
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_tou_precondition(c, 64.0, "s1"))
            assert engine.minutes_since_mode_set("cool", clock.now, frozenset({"tou", "user", "ode_guard"})) is None
        assert engine.comfort_mode_changes == []

    @pytest.mark.parametrize(("cmd", "real", "expected"), [("cool", "heat", "cool"), ("heat", "cool", "heat")])
    def test_recovery_to_a_real_mode_then_a_real_change_records_once(self, cmd, real, expected):
        engine = _unavailable_engine(cmd)
        clock = _Clock()
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), engine._mode_change_context("restore"):
            _run(engine._set_hvac_mode(cmd, reason="while unavailable"))
        assert engine.comfort_mode_changes == []
        engine._test_state.state = real  # thermostat comes back reporting a real, different mode
        clock.advance(minutes=5)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), engine._mode_change_context("restore"):
            _run(engine._set_hvac_mode(expected, reason="after recovery"))
        assert len(engine.comfort_mode_changes) == 1
        assert engine.comfort_mode_changes[0]["from_mode"] == real
        assert len(_events(engine, "thermostat_mode_set")) == 1


class TestReactiveTickRecordsHolds:
    @pytest.mark.parametrize(
        ("day", "mode", "ran", "readings", "recover", "family"),
        [
            ("hot", "cool", "cool", (69.0, 67.0), 71.0, "heating"),
            ("cold", "heat", "heat", (73.0, 75.0), 71.0, "cooling"),
        ],
    )
    def test_hold_starts_and_ends_on_reactive_ticks(self, caplog, day, mode, ran, readings, recover, family):
        engine, clock = _scene(day=day, mode=mode, indoor=71.0, ran=ran, ran_min=2.0)
        _capture_events(engine)
        calls_before = engine.hass.services.async_call.call_count
        with caplog.at_level("INFO", logger=_AUTOMATION):
            for r in readings:
                clock.advance(seconds=30)
                _reactive(engine, clock, r)
            held_info = [x for x in caplog.records if "Comfort edge switch held" in x.getMessage()]
        assert _mode(engine) == mode
        assert len(held_info) == 1, "one INFO per hold, not per reading"
        assert len(_events(engine, "comfort_switch_held")) == 1
        assert engine._last_comfort_hold is not None and engine._last_comfort_hold["candidate_family"] == family
        assert len(engine.comfort_holds) == 1 and engine.comfort_holds[0]["end_ts"] is None
        label = _coordinator_class()._comfort_family_hold_label(engine, clock.now, "fahrenheit")
        assert label and label.startswith(f"holding {family}")
        # recovery above/below the edge closes the episode as 'recovered' on a reactive tick
        clock.advance(seconds=30)
        _reactive(engine, clock, recover)
        assert engine._last_comfort_hold is None
        assert engine.comfort_holds[0]["end_reason"] == "recovered"
        assert engine.hass.services.async_call.call_count == calls_before, "no extra service calls"
        # control_quality counts it
        from custom_components.climate_advisor.control_quality import build_control_quality

        cq = build_control_quality(
            now=clock.now,
            mode_changes=engine.comfort_mode_changes,
            chart_entries=[],
            comfort_holds=engine.comfort_holds,
        )
        assert cq["windows"]["24h"]["settling_guard"]["hold_episodes"] == 1

    def test_no_hold_means_the_reactive_tick_stays_silent(self, caplog):
        engine, clock = _scene(day="hot", mode="cool", indoor=71.0, ran=None)
        with caplog.at_level("DEBUG", logger=_AUTOMATION):
            for r in (70.0, 71.0, 72.0):
                _reactive(engine, clock, r)
        assert not [x for x in caplog.records if "hold" in x.getMessage().lower()]
        assert engine.comfort_holds == []


class TestNegativeRanMinutes:
    def test_future_equipment_stamp_is_unknown_not_just_ran(self):
        from tests.test_comfort_settling_guard import _stamp_ran

        engine = _make_engine(indoor=67.0, mode="cool")
        _stamp_ran(engine, "cool", -30.0)  # a timestamp 30 min in the future (clock jump / restore)
        clock = _Clock()
        _apply(engine, clock, "mild", 67.0)
        assert _mode(engine) == "heat", "an unknown ran time never holds"
        assert engine._last_comfort_hold is None

    def test_leaf_treats_negative_ran_as_unknown(self):
        assert _leaf(indoor=67.0, opposite_ran_min_ago=-5.0, native_family=None).outcome is ComfortFamilyOutcome.SWITCH
