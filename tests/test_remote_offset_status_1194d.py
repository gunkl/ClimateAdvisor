"""Issue #1194 increment (d): Status "clamped", and the comfort target vs commanded setpoint split.

Occupant outcome: on an offset night the hallway thermostat is DELIBERATELY set up to 4 F above the bedroom's sleep
target so the furnace starts. The Status card used to show "70F (CA: 66F)", which reads like the thermostat and
Climate Advisor disagree (or something is broken), and the chart drew the commanded 70 F as if the sleep band had
moved. Now Status says "Bedroom 66F target - thermostat set 70F (+4F, clamped at the +4F cap)" and the chart draws the
room's target and the thermostat's command as two lines.

Everything is read from ONE description of the loop (``AutomationEngine.remote_offset_state``): the engine stores the
loop's last decision reason, and Status, the chart log, the AI context and the diagnostic snapshot only describe it.
The real engine runs through the headless harness (the same one the (a) tests use).
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.climate_advisor.remote_offset import (
    ClampLimit,
    RemoteLoopState,
    RemoteOffsetGate,
    RemoteOffsetState,
    binding_limit,
    classify_loop_state,
    remote_offset_text,
    remote_offset_view,
    remote_offset_view_for,
)
from tests.test_remote_offset_1194a import BEDROOM, _classification  # noqa: F401
from tests.test_remote_offset_1194a import Env as _BaseEnv


class Env(_BaseEnv):
    """The (a) harness Env; reads of the loop run on the VIRTUAL clock too (never the wall clock)."""

    def state(self):
        with self.sched.installed():
            return self.engine.remote_offset_state()


def _st(**kw) -> RemoteOffsetState:
    base = {
        "state": RemoteLoopState.ACTIVE,
        "context": "sleep",
        "mode": "heat",
        "comfort_target_f": 66.0,
        "commanded_f": 70.0,
        "offset_f": 4.0,
        "gate_reason": None,
        "clamp_limit": None,
        "last_reason": "raised",
        "remote_value_f": 64.0,
        "remote_age_s": 30.0,
        "cap_limit_f": 4.0,
    }
    base.update(kw)
    return RemoteOffsetState(**base)


def _raise_to_cap(env, target=66.0, mode="heat", cold=58.0):
    """Drive the real loop until it sits at the cap (the bedroom stays far from target)."""
    env.write(target, mode=mode)
    for _ in range(4):
        env.check()
        env.advance(400)
        env.set_bedroom(cold)
    env.check()


# ---------------------------------------------------------------------------
# Pure: state classification, limits, text, view
# ---------------------------------------------------------------------------


class TestClassify:
    def _c(self, **kw):
        args = {
            "in_context": True,
            "fresh": True,
            "gate": RemoteOffsetGate.PROCEED,
            "last_reason": "in_band",
            "offset_f": 0.0,
        }
        args.update(kw)
        return classify_loop_state(**args)

    def test_each_state(self):
        assert self._c(in_context=False) is RemoteLoopState.INACTIVE
        assert self._c(fresh=False) is RemoteLoopState.STALE
        assert self._c(gate=RemoteOffsetGate.CLOSED) is RemoteLoopState.GATED
        assert self._c(gate=RemoteOffsetGate.FREEZE) is RemoteLoopState.FROZEN
        assert self._c(last_reason="at_cap", offset_f=4.0) is RemoteLoopState.CLAMPED
        assert self._c(last_reason="raised", offset_f=2.0) is RemoteLoopState.ACTIVE
        assert self._c(last_reason="awaiting_startup", offset_f=2.0) is RemoteLoopState.ACTIVE
        assert self._c(last_reason="in_band", offset_f=2.0) is RemoteLoopState.HOLDING
        assert self._c(last_reason="in_band", offset_f=0.0) is RemoteLoopState.HOLDING

    def test_order_stale_beats_gate_beats_clamp(self):
        assert self._c(fresh=False, gate=RemoteOffsetGate.CLOSED, last_reason="at_cap") is RemoteLoopState.STALE
        assert self._c(gate=RemoteOffsetGate.FREEZE, last_reason="at_cap") is RemoteLoopState.FROZEN

    def test_binding_limit_names_the_tighter_limit(self):
        assert binding_limit("heat", 66.0, floor=66.0, ceiling=72.0, cap_f=4.0) is ClampLimit.CAP
        assert binding_limit("heat", 66.0, floor=66.0, ceiling=68.0, cap_f=4.0) is ClampLimit.BAND_EDGE
        assert binding_limit("cool", 72.0, floor=66.0, ceiling=72.0, cap_f=4.0) is ClampLimit.CAP
        assert binding_limit("cool", 72.0, floor=70.0, ceiling=72.0, cap_f=4.0) is ClampLimit.BAND_EDGE
        # exactly equal: the cap is what stops it
        assert binding_limit("heat", 66.0, floor=66.0, ceiling=70.0, cap_f=4.0) is ClampLimit.CAP


class TestText:
    def test_active(self):
        t = remote_offset_text(_st(), "fahrenheit")
        assert t == "Bedroom 66°F target - thermostat set 70°F (+4°F, raising)"

    def test_holding_with_offset(self):
        t = remote_offset_text(
            _st(state=RemoteLoopState.HOLDING, last_reason="in_band", offset_f=2.0, commanded_f=68.0), "fahrenheit"
        )
        assert t == "Bedroom 66°F target - thermostat set 68°F (+2°F, holding)"

    def test_holding_without_offset_says_nothing(self):
        assert (
            remote_offset_text(
                _st(state=RemoteLoopState.HOLDING, offset_f=0.0, commanded_f=66.0, last_reason="in_band"), "fahrenheit"
            )
            is None
        )

    def test_clamped_at_cap(self):
        t = remote_offset_text(
            _st(state=RemoteLoopState.CLAMPED, clamp_limit=ClampLimit.CAP, last_reason="at_cap"), "fahrenheit"
        )
        assert t == "Bedroom 66°F target - thermostat set 70°F (+4°F, clamped at the +4°F cap)"

    def test_clamped_at_opposite_band_edge_heat_and_cool(self):
        heat = remote_offset_text(
            _st(state=RemoteLoopState.CLAMPED, clamp_limit=ClampLimit.BAND_EDGE, offset_f=1.0, commanded_f=67.0),
            "fahrenheit",
        )
        assert heat == "Bedroom 66°F target - thermostat set 67°F (+1°F, clamped at the comfort band ceiling)"
        cool = remote_offset_text(
            _st(
                state=RemoteLoopState.CLAMPED,
                mode="cool",
                clamp_limit=ClampLimit.BAND_EDGE,
                comfort_target_f=72.0,
                commanded_f=71.0,
                offset_f=-1.0,
            ),
            "fahrenheit",
        )
        assert cool == "Bedroom 72°F target - thermostat set 71°F (-1°F, clamped at the comfort band floor)"

    def test_clamped_with_no_room_at_all_still_explains(self):
        t = remote_offset_text(
            _st(state=RemoteLoopState.CLAMPED, clamp_limit=ClampLimit.BAND_EDGE, offset_f=0.0, commanded_f=66.0),
            "fahrenheit",
        )
        assert t == "Bedroom 66°F target - thermostat set 66°F (no offset, clamped at the comfort band ceiling)"

    def test_frozen_gated_stale_each_get_a_short_reason(self):
        frozen = remote_offset_text(_st(state=RemoteLoopState.FROZEN, gate_reason="debounce"), "fahrenheit")
        assert frozen == "Bedroom 66°F target - thermostat set 70°F (+4°F, held, window or door just opened)"
        gated = remote_offset_text(
            _st(state=RemoteLoopState.GATED, gate_reason="door_window_pause", offset_f=None, commanded_f=None),
            "fahrenheit",
        )
        assert gated == "Bedroom offset off: paused for an open window or door"
        stale = remote_offset_text(_st(state=RemoteLoopState.STALE, offset_f=None, commanded_f=None), "fahrenheit")
        assert stale == "Bedroom sensor not reporting - offset not in use"

    def test_frozen_with_unknown_offset_still_says_why(self):
        t = remote_offset_text(
            _st(state=RemoteLoopState.FROZEN, gate_reason="debounce", offset_f=None, commanded_f=None), "fahrenheit"
        )
        assert t == "Bedroom offset held: window or door just opened"

    def test_inactive_says_nothing(self):
        assert remote_offset_text(_st(state=RemoteLoopState.INACTIVE), "fahrenheit") is None

    def test_never_uses_the_402_disagreement_wording(self):
        for state in RemoteLoopState:
            text = remote_offset_text(_st(state=state, gate_reason="override"), "fahrenheit") or ""
            assert "CA:" not in text


class TestViewUnits:
    def test_celsius_view_converts_absolutes_and_deltas_separately(self):
        v = remote_offset_view(_st(offset_f=3.6, commanded_f=69.6, cap_limit_f=3.6), "celsius")
        assert v["comfort_target"] == pytest.approx(18.9, abs=0.05)  # 66 F absolute -> C
        assert v["commanded"] == pytest.approx(20.9, abs=0.05)  # 69.6 F
        assert v["offset_applied"] == pytest.approx(2.0, abs=0.01)  # 3.6 F DELTA -> 2.0 C, not -15.8 C
        assert v["offset_applied_f"] == pytest.approx(3.6)
        assert v["expected_setpoint"] == pytest.approx(20.9, abs=0.05)
        assert v["unit"] == "celsius"

    def test_celsius_text_uses_celsius_degrees(self):
        text = remote_offset_text(
            _st(
                state=RemoteLoopState.CLAMPED,
                clamp_limit=ClampLimit.CAP,
                offset_f=3.6,
                commanded_f=69.6,
                cap_limit_f=3.6,
            ),
            "celsius",
        )
        assert text == "Bedroom 19°C target - thermostat set 21°C (+2°C, clamped at the +2°C cap)"

    def test_cool_offset_is_negative(self):
        v = remote_offset_view(_st(mode="cool", comfort_target_f=72.0, commanded_f=68.0, offset_f=-4.0), "fahrenheit")
        assert v["offset_applied"] == -4.0 and v["offset_applied_f"] == -4.0 and v["offset_active"] is True

    def test_no_offset_leaves_expected_setpoint_empty(self):
        v = remote_offset_view(_st(offset_f=0.0, state=RemoteLoopState.HOLDING, commanded_f=66.0), "fahrenheit")
        assert v["expected_setpoint"] is None and v["offset_active"] is False

    def test_view_for_tolerates_stubs(self):
        assert remote_offset_view_for(None, "fahrenheit") is None
        assert remote_offset_view_for(MagicMock(), "fahrenheit") is None
        assert remote_offset_view_for(SimpleNamespace(remote_offset_state=lambda: None), "fahrenheit") is None

        def boom():
            raise RuntimeError("x")

        assert remote_offset_view_for(SimpleNamespace(remote_offset_state=boom), "fahrenheit") is None


# ---------------------------------------------------------------------------
# The real engine: one description of the loop, every state
# ---------------------------------------------------------------------------


class TestEngineState:
    def test_no_remote_sensor_configured_is_none(self):
        env = Env(config={"sleep_indoor_temp_entity": None})
        assert env.state() is None

    def test_primary_sensor_in_use_is_inactive(self):
        from tests.test_remote_offset_1194a import DAY

        env = Env(start=DAY)
        env.write(66.0)
        st = env.state()
        assert st is not None and st.state is RemoteLoopState.INACTIVE
        assert remote_offset_text(st, "fahrenheit") is None

    def test_active_after_a_raise_carries_the_whole_picture(self):
        env = Env(bedroom_f=64.0)
        env.write(66.0)
        env.check()  # raise to +2
        st = env.state()
        assert st.state is RemoteLoopState.ACTIVE
        assert (st.mode, st.context) == ("heat", "sleep")
        assert st.comfort_target_f == 66.0 and st.commanded_f == 68.0 and st.offset_f == 2.0
        assert st.last_reason == "raised"
        assert st.remote_value_f == 64.0 and st.remote_age_s is not None and st.remote_age_s >= 0
        assert remote_offset_text(st, "fahrenheit") == "Bedroom 66°F target - thermostat set 68°F (+2°F, raising)"

    def test_awaiting_startup_is_active_with_its_reason(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        env.check()  # raise
        env.check()  # inside the startup wait
        st = env.state()
        assert st.last_reason == "awaiting_startup" and st.state is RemoteLoopState.ACTIVE
        assert "waiting for the thermostat to start" in remote_offset_text(st, "fahrenheit")

    def test_holding_once_the_bedroom_is_in_band(self):
        env = Env(bedroom_f=64.0)
        env.write(66.0)
        env.check()  # +2
        env.advance(400)
        env.set_bedroom(66.0)
        env.check()
        st = env.state()
        assert st.state is RemoteLoopState.HOLDING and st.last_reason == "in_band" and st.offset_f == 2.0

    def test_clamped_at_the_plus_four_cap(self):
        env = Env(bedroom_f=58.0)
        _raise_to_cap(env)
        st = env.state()
        assert st.state is RemoteLoopState.CLAMPED and st.clamp_limit is ClampLimit.CAP
        assert st.last_reason == "at_cap" and st.offset_f == 4.0 and st.commanded_f == 70.0
        assert remote_offset_text(st, "fahrenheit") == (
            "Bedroom 66°F target - thermostat set 70°F (+4°F, clamped at the +4°F cap)"
        )

    def test_clamped_at_the_opposite_band_edge(self):
        env = Env(bedroom_f=58.0, config={"sleep_cool": 67.0})  # ceiling 67 leaves room for +1 only
        _raise_to_cap(env)
        st = env.state()
        assert st.state is RemoteLoopState.CLAMPED and st.clamp_limit is ClampLimit.BAND_EDGE
        assert st.offset_f == 1.0 and st.commanded_f == 67.0
        assert "clamped at the comfort band ceiling" in remote_offset_text(st, "fahrenheit")

    def test_cool_clamped_at_the_floor_and_at_the_cap(self):
        env = Env(mode="cool", bedroom_f=80.0, hall_f=71.0)
        env.engine._current_classification = _classification("cool")
        _raise_to_cap(env, target=72.0, mode="cool", cold=80.0)
        st = env.state()
        assert st.state is RemoteLoopState.CLAMPED and st.clamp_limit is ClampLimit.CAP
        assert st.offset_f == -4.0 and st.commanded_f == 68.0
        env2 = Env(mode="cool", bedroom_f=80.0, hall_f=71.0, config={"sleep_heat": 70.0})  # floor 70 -> room 2
        env2.engine._current_classification = _classification("cool")
        _raise_to_cap(env2, target=72.0, mode="cool", cold=80.0)
        st2 = env2.state()
        assert st2.clamp_limit is ClampLimit.BAND_EDGE and st2.offset_f == -2.0
        assert "clamped at the comfort band floor" in remote_offset_text(st2, "fahrenheit")

    def test_stale_remote_reading_falls_back_and_says_so(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        env.check()  # +2 in force
        env.advance(60)
        env.set_bedroom(58.0, reported=env.sched.now() - timedelta(hours=3))
        env.check()
        st = env.state()
        assert st.state is RemoteLoopState.STALE and st.last_reason == "stale"
        assert st.remote_age_s is not None and st.remote_age_s > 2 * 3600
        assert remote_offset_text(st, "fahrenheit") == "Bedroom sensor not reporting - offset not in use"

    def test_gated_by_a_door_window_pause(self):
        env = Env(bedroom_f=64.0)
        env.write(66.0)
        env.check()
        env.engine._paused_by_door = True
        st = env.state()
        assert st.state is RemoteLoopState.GATED and st.gate_reason == "door_window_pause"
        assert remote_offset_text(st, "fahrenheit") == "Bedroom offset off: paused for an open window or door"

    @pytest.mark.parametrize(
        ("setup", "reason"),
        [
            (lambda e: setattr(e, "_occupancy_mode", "away"), "away"),
            (lambda e: setattr(e, "_manual_override_active", True), "occupant_change"),
            (lambda e: setattr(e, "_natural_vent_active", True), "fan"),
        ],
    )
    def test_gate_reason_per_owner(self, setup, reason):
        env = Env(bedroom_f=64.0)
        env.write(66.0)
        setup(env.engine)
        st = env.state()
        assert st.state is RemoteLoopState.GATED
        assert st.gate_reason == reason

    def test_frozen_inside_the_debounce_keeps_the_held_offset(self):
        env = Env(bedroom_f=64.0)
        env.write(66.0)
        env.check()  # +2
        env.engine._any_monitored_sensor_holding_open = lambda: True
        env.engine._sensor_debounce_pending_callback = lambda: True
        st = env.state()
        assert st.state is RemoteLoopState.FROZEN and st.gate_reason == "debounce"
        assert st.offset_f == 2.0 and st.commanded_f == 68.0
        assert remote_offset_text(st, "fahrenheit") == (
            "Bedroom 66°F target - thermostat set 68°F (+2°F, held, window or door just opened)"
        )

    def test_window_open_past_the_debounce_is_gated(self):
        env = Env(bedroom_f=64.0)
        env.write(66.0)
        env.engine._any_monitored_sensor_holding_open = lambda: True
        env.engine._sensor_debounce_pending_callback = lambda: False
        st = env.state()
        assert st.state is RemoteLoopState.GATED and st.gate_reason == "window_open"

    def test_state_is_a_pure_read(self):
        """Describing the loop writes nothing and moves nothing (the display layer never decides)."""
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        env.check()
        writes = len(env.sp_f())
        before = (env.engine._remote_offset_f, env.engine._remote_offset_applied_f, env.engine._remote_last_reason)
        for _ in range(3):
            env.state()
        assert len(env.sp_f()) == writes
        assert (
            env.engine._remote_offset_f,
            env.engine._remote_offset_applied_f,
            env.engine._remote_last_reason,
        ) == before

    def test_last_reason_is_stored_by_the_loop_and_cleared_with_the_offset(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        assert env.engine._remote_last_reason is None
        env.check()
        assert env.engine._remote_last_reason == "raised"
        env.engine._reset_remote_offset()
        assert env.engine._remote_last_reason is None


# ---------------------------------------------------------------------------
# Status payload (real view + real engine)
# ---------------------------------------------------------------------------


def _status_for(env, *, thermostat_setpoint, unit="fahrenheit"):
    from tests.test_api import _make_classification, _simulate_status_get

    state = MagicMock()
    state.state = "heat"
    state.attributes = {"temperature": thermostat_setpoint, "target_temp_low": None, "target_temp_high": None}
    coord = MagicMock()
    coord.config = {
        "temp_unit": unit,
        "comfort_heat": 70.0,
        "comfort_cool": 75.0,
        "sleep_heat": 66.0,
        "sleep_cool": 72.0,
        "sleep_time": "20:30",
        "wake_time": "06:30",
    }
    coord.data = {}
    coord._get_indoor_temp.return_value = 64.0
    coord._last_outdoor_temp = None
    coord.automation_enabled = True
    coord._occupancy_mode = "home"
    coord.current_classification = _make_classification()
    coord.compute_nat_vent_cycling_band.return_value = {
        "nat_vent_target": None,
        "nat_vent_on_threshold": None,
        "nat_vent_off_threshold": None,
    }
    coord._compute_contact_details.return_value = []
    coord.automation_engine = env.engine
    with env.sched.installed():
        return _simulate_status_get(coord, state)


class TestStatusPayload:
    def test_offset_night_payload_splits_target_from_command(self):
        env = Env(bedroom_f=58.0)
        _raise_to_cap(env)
        body = _status_for(env, thermostat_setpoint=70)
        ro = body["remote_offset"]
        assert ro["state"] == "clamped" and ro["clamp_limit"] == "cap"
        assert ro["comfort_target"] == 66.0 and ro["commanded"] == 70.0
        assert ro["offset_applied"] == 4.0 and ro["offset_applied_f"] == 4.0
        assert ro["context"] == "sleep" and ro["context_label"] == "Bedroom"
        assert ro["remote_reading"] == 58.0 and ro["remote_reading_age_s"] is not None
        assert ro["last_decision_reason"] == "at_cap"
        assert ro["text"] == "Bedroom 66°F target - thermostat set 70°F (+4°F, clamped at the +4°F cap)"

    def test_402_comparison_is_against_the_commanded_value_not_the_band_edge(self):
        """The frontend compares the thermostat with ``expected_setpoint`` when it is set: the commanded 70, not 66."""
        env = Env(bedroom_f=58.0)
        _raise_to_cap(env)
        body = _status_for(env, thermostat_setpoint=70)
        assert body["remote_offset"]["expected_setpoint"] == 70.0
        assert body["current_setpoint"] == 70
        assert body["ca_target_heat"] == 66.0  # the band edge is still published, for the real-disagreement check
        assert abs(body["current_setpoint"] - body["remote_offset"]["expected_setpoint"]) <= 1  # no (CA: ...) shown

    def test_a_real_mismatch_is_still_visible_to_the_comparison(self):
        env = Env(bedroom_f=58.0)
        _raise_to_cap(env)
        body = _status_for(env, thermostat_setpoint=74)  # the thermostat reports something CA did not command
        assert body["remote_offset"]["expected_setpoint"] == 70.0
        assert abs(body["current_setpoint"] - body["remote_offset"]["expected_setpoint"]) > 1  # -> "(CA: 70)"

    def test_no_offset_keeps_the_band_edge_comparison(self):
        env = Env(bedroom_f=65.5)
        env.write(66.0)
        env.check()
        body = _status_for(env, thermostat_setpoint=66)
        assert body["remote_offset"]["expected_setpoint"] is None
        assert body["remote_offset"]["text"] is None

    def test_celsius_install(self):
        env = Env(unit="celsius", bedroom_f=58.0)
        _raise_to_cap(env)
        body = _status_for(env, thermostat_setpoint=21.1, unit="celsius")
        ro = body["remote_offset"]
        assert ro["unit"] == "celsius"
        assert ro["comfort_target"] == pytest.approx(18.9, abs=0.1)
        assert ro["commanded"] == pytest.approx(20.9, abs=0.1)
        assert ro["offset_applied"] == pytest.approx(2.0, abs=0.01)
        assert "°C" in ro["text"] and "°F" not in ro["text"]

    def test_install_without_a_remote_sensor_publishes_null(self):
        env = Env(config={"sleep_indoor_temp_entity": None})
        body = _status_for(env, thermostat_setpoint=66)
        assert body["remote_offset"] is None


class TestFrontend:
    """No JS runtime here: pin what the page reads (behaviour: tests/ui/remote-offset-status.spec.js)."""

    def _html(self):
        from pathlib import Path

        return (
            Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "frontend" / "index.html"
        ).read_text(encoding="utf-8")

    def test_status_card_reads_the_payload_fields(self):
        html = self._html()
        assert "data.remote_offset && data.remote_offset.text ?" in html
        assert "ro.expected_setpoint" in html
        assert "remote-offset-line" in html
        assert "(CA: ${caValSingle})" in html  # the #402 comparison is kept for a real disagreement

    def test_chart_draws_the_commanded_setpoint_as_its_own_series(self):
        html = self._html()
        assert "chartData.commanded_setpoint_history" in html
        assert "const _hasCommanded = commandedPts.some(p => p.y != null);" in html and "if (_hasCommanded) {" in html
        assert "label: 'Thermostat set'" in html
        assert "d.label === 'Thermostat set'" in html  # the Target toggle drives it too


# ---------------------------------------------------------------------------
# Chart: comfort target and commanded setpoint as separate series
# ---------------------------------------------------------------------------


class TestChart:
    def test_chart_log_row_carries_comfort_target_only_when_given(self, tmp_path):
        from custom_components.climate_advisor.chart_log import ChartStateLog

        log = ChartStateLog(tmp_path)
        log.append(hvac="heat", fan=False, indoor=64.0, outdoor=40.0, setpoint=70.0, comfort_target=66.0)
        log.append(hvac="heat", fan=False, indoor=66.0, outdoor=40.0, setpoint=66.0)
        rows = log.get_entries("24h")
        assert rows[-2]["comfort_target"] == 66.0 and rows[-2]["setpoint"] == 70.0
        assert "comfort_target" not in rows[-1]

    def test_extractors_split_target_from_command(self):
        from custom_components.climate_advisor.coordinator import (
            _extract_historical_commanded_setpoint,
            _extract_historical_effective_target,
        )

        rows = [
            {"ts": "t1", "setpoint": 66.0},
            {"ts": "t2", "setpoint": 70.0, "comfort_target": 66.0},
            {"ts": "t3", "setpoint": 66.0},
        ]
        assert [r["target"] for r in _extract_historical_effective_target(rows)] == [66.0, 66.0, 66.0]
        assert [r["target"] for r in _extract_historical_commanded_setpoint(rows)] == [None, 70.0, None]

    @pytest.mark.parametrize(("unit", "target", "commanded"), [("fahrenheit", 66.0, 70.0), ("celsius", 18.9, 21.1)])
    def test_chart_payload_has_both_series_in_display_unit(self, unit, target, commanded):
        from tests.test_chart_celsius import (
            _NOW,
            _daily_6day_forecast,
            _make_full_chart_coord,
            _raw_entry,
            _run_chart,
        )

        coord = _make_full_chart_coord(daily_forecast_full=_daily_6day_forecast(), temp_unit=unit)
        rows = [_raw_entry(_NOW - timedelta(hours=h)) for h in (3, 2, 1)]
        rows[0]["setpoint"] = 66.0
        rows[1]["setpoint"] = 70.0
        rows[1]["comfort_target"] = 66.0
        rows[2]["setpoint"] = 66.0
        coord._chart_log.get_entries.return_value = rows
        result = _run_chart(coord, "24h")
        eff = [p["target"] for p in result["effective_target_history"]]
        cmd = [p["target"] for p in result["commanded_setpoint_history"]]
        assert eff == [pytest.approx(target, abs=0.06)] * 3  # the Target line never moves: not a band change
        assert cmd[0] is None and cmd[2] is None
        assert cmd[1] == pytest.approx(commanded, abs=0.06)

    def test_every_chart_log_row_site_records_the_comfort_target(self):
        """All four ``chart_log.append`` sites (30-min poll, classification change, override, hvac_action change)."""
        import ast
        from pathlib import Path

        src = (
            Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "coordinator.py"
        ).read_text(encoding="utf-8")
        calls = [
            n
            for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "append"
            and isinstance(n.func.value, ast.Attribute)
            and n.func.value.attr == "_chart_log"
        ]
        assert len(calls) == 4
        for call in calls:
            assert "comfort_target" in {k.arg for k in call.keywords}, f"chart_log.append at line {call.lineno}"

    def test_read_chart_comfort_target_only_during_an_offset(self):
        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        env = Env(bedroom_f=58.0)
        env.write(66.0)
        shim = SimpleNamespace(automation_engine=env.engine)
        raw = ClimateAdvisorCoordinator._read_chart_comfort_target

        def read(c):
            with env.sched.installed():
                return raw(c)

        assert read(shim) is None  # offset 0
        env.check()  # +2
        assert read(shim) == 66.0
        env.engine._reset_remote_offset(applied_unknown=True)
        assert read(shim) is None  # a gate cleared it
        assert read(SimpleNamespace(automation_engine=MagicMock())) is None  # stub engine


# ---------------------------------------------------------------------------
# Diagnostics and AI context: the same fields, once each
# ---------------------------------------------------------------------------


class TestDiagnosticsAndAI:
    def _coord(self, env):
        coord = MagicMock()
        coord.automation_engine = env.engine
        coord.config = {"temp_unit": "fahrenheit", "climate_entity": "climate.test_thermostat"}
        coord.data = {"hvac_mode": "heat"}
        return coord

    def test_diagnostic_snapshot_carries_the_view(self):
        from custom_components.climate_advisor.diagnostic_snapshot import build_diagnostic_snapshot

        env = Env(bedroom_f=58.0)
        _raise_to_cap(env)
        hass = MagicMock()
        hass.states.get.return_value = None
        with env.sched.installed():
            snap = build_diagnostic_snapshot(hass, self._coord(env), include_control_quality=False)
        ro = snap["remote_offset"]
        assert ro["state"] == "clamped" and ro["detail"] == "cap" and ro["clamp_limit"] == "cap"
        assert ro["comfort_target"] == 66.0 and ro["commanded"] == 70.0 and ro["offset_applied"] == 4.0
        assert ro["last_decision_reason"] == "at_cap" and ro["remote_reading"] == 58.0
        assert ro["remote_reading_age_s"] is not None

    def test_diagnostic_snapshot_omits_the_key_without_a_remote_sensor(self):
        from custom_components.climate_advisor.diagnostic_snapshot import build_diagnostic_snapshot

        env = Env(config={"sleep_indoor_temp_entity": None})
        hass = MagicMock()
        hass.states.get.return_value = None
        with env.sched.installed():
            snap = build_diagnostic_snapshot(hass, self._coord(env), include_control_quality=False)
        assert "remote_offset" not in snap

    def test_ai_context_lists_the_loop_state(self):
        import asyncio

        from custom_components.climate_advisor.ai_skills_context import build_hvac_entity_context

        env = Env(bedroom_f=58.0)
        _raise_to_cap(env)
        hass = MagicMock()
        hass.states.get.return_value = MagicMock(attributes={"current_temperature": 67})
        with env.sched.installed():
            ctx = asyncio.run(build_hvac_entity_context(hass, self._coord(env)))
        assert "remote_sensor_offset:" in ctx
        assert "state: 'clamped'" in ctx and "clamp_limit: 'cap'" in ctx
        assert "comfort_target: 66.0" in ctx and "commanded: 70.0" in ctx
        assert "last_decision_reason: 'at_cap'" in ctx

    def test_ai_context_unchanged_without_a_remote_sensor(self):
        import asyncio

        from custom_components.climate_advisor.ai_skills_context import build_hvac_entity_context

        coord = MagicMock()
        coord.data = {"hvac_mode": "heat", "target_temp_low": 68.0, "target_temp_high": 76.0}
        coord.config = {"climate_entity": "climate.t"}
        hass = MagicMock()
        hass.states.get.return_value = MagicMock(attributes={"current_temperature": 70})
        ctx = asyncio.run(build_hvac_entity_context(hass, coord))
        assert "remote_sensor_offset" not in ctx


# ---------------------------------------------------------------------------
# Verification fixes (Issue #1194 d review)
# ---------------------------------------------------------------------------


def _failing_writes(env):
    orig = env.hass.services.async_call

    async def failing(domain, service, data=None, *a, **k):
        if service == "set_temperature":
            raise RuntimeError("thermostat cloud unreachable")
        return await orig(domain, service, data, *a, **k)

    env.hass.services.async_call = failing


class TestNoClaimWithoutKnowledge:
    """Occupant outcome: when the offset is no longer intended (bedroom sensor silent, an owner took over) but the
    thermostat may still hold it, Status must not claim the thermostat is at the plain target and must not hide a
    stranded offset from the #402 "(CA: X)" check."""

    def test_stale_with_a_refused_write_keeps_the_stranded_offset_visible(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        env.check()  # +2 on the thermostat (68)
        _failing_writes(env)
        env.advance(60)
        env.set_bedroom(58.0, reported=env.sched.now() - timedelta(hours=3))
        env.check()  # stale -> the loop tries to write the plain 66 and the thermostat refuses
        st = env.state()
        assert st.state is RemoteLoopState.STALE
        assert st.offset_f == 2.0  # CA knows the thermostat still holds 68
        view = remote_offset_view(st, "fahrenheit")
        assert view["expected_setpoint"] is None  # -> the page compares 68 with the band edge: "(CA: 66)"
        assert "plain target" not in view["text"]
        assert view["text"] == "Bedroom sensor not reporting - offset not in use"

    def test_gated_before_the_loop_resets_does_not_expect_the_offset(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        env.check()  # +2
        env.engine._paused_by_door = True  # an owner, before the loop's next check resets its bookkeeping
        st = env.state()
        assert st.state is RemoteLoopState.GATED and st.offset_f == 2.0
        assert remote_offset_view(st, "fahrenheit")["expected_setpoint"] is None

    def test_intended_offset_states_still_expect_the_commanded_value(self):
        for state in (RemoteLoopState.ACTIVE, RemoteLoopState.HOLDING, RemoteLoopState.CLAMPED, RemoteLoopState.FROZEN):
            assert remote_offset_view(_st(state=state), "fahrenheit")["expected_setpoint"] == 70.0, state
        for state in (RemoteLoopState.GATED, RemoteLoopState.STALE, RemoteLoopState.INACTIVE):
            assert remote_offset_view(_st(state=state), "fahrenheit")["expected_setpoint"] is None, state

    def test_frontend_uses_the_commanded_value_only_in_the_offset_mode(self):
        html = TestFrontend()._html()
        assert "(ro && ro.expected_setpoint != null && ro.mode === data.hvac_mode)" in html


def _bucket_rows(base):
    """Two offset rows (thermostat 70, room target 66) and one plain row (66) in one hour, internal F."""
    return [
        {"ts": (base + timedelta(minutes=0)).isoformat(), "hvac": "heating", "setpoint": 70.0, "comfort_target": 66.0},
        {"ts": (base + timedelta(minutes=20)).isoformat(), "hvac": "heating", "setpoint": 70.0, "comfort_target": 66.0},
        {"ts": (base + timedelta(minutes=40)).isoformat(), "hvac": "idle", "setpoint": 66.0},
    ]


class TestChartBuckets:
    """Occupant outcome: on the 7d / 30d / 1y ranges an offset night must not show the Target line jumping to the
    thermostat's commanded value (it would read as the sleep temperature moving)."""

    def _log(self, tmp_path):
        from custom_components.climate_advisor.chart_log import ChartStateLog

        return ChartStateLog(tmp_path)

    @pytest.mark.parametrize("bucket", ["_bucket_hourly", "_bucket_daily"])
    def test_bucket_averages_the_comfort_target_like_the_setpoint(self, tmp_path, bucket):
        from datetime import UTC, datetime

        log = self._log(tmp_path)
        rows = _bucket_rows(datetime(2026, 1, 10, 3, 0, tzinfo=UTC))
        [b] = getattr(log, bucket)(rows)
        assert b["setpoint"] == round((70 + 70 + 66) / 3, 1)  # what the thermostat was told, averaged as before
        assert b["comfort_target"] == 66.0  # the plain row's target IS its setpoint (66), never a band change

    @pytest.mark.parametrize("bucket", ["_bucket_hourly", "_bucket_daily"])
    def test_bucket_without_an_offset_row_has_no_comfort_target(self, tmp_path, bucket):
        from datetime import UTC, datetime

        log = self._log(tmp_path)
        rows = [r for r in _bucket_rows(datetime(2026, 1, 10, 3, 0, tzinfo=UTC)) if "comfort_target" not in r]
        [b] = getattr(log, bucket)(rows)
        assert "comfort_target" not in b and b["setpoint"] == 66.0

    def test_mixed_targets_average_per_row(self, tmp_path):
        from datetime import UTC, datetime

        log = self._log(tmp_path)
        base = datetime(2026, 1, 10, 3, 0, tzinfo=UTC)
        rows = [
            {"ts": base.isoformat(), "setpoint": 70.0, "comfort_target": 66.0},
            {"ts": (base + timedelta(minutes=30)).isoformat(), "setpoint": 72.0},  # band moved to 72, no offset
            {"ts": (base + timedelta(minutes=45)).isoformat(), "comfort_target": 66.0},  # thermostat unreadable
        ]
        [b] = log._bucket_hourly(rows)
        assert b["comfort_target"] == round((66 + 72 + 66) / 3, 1)
        assert b["setpoint"] == 71.0

    @pytest.mark.parametrize(
        ("unit", "range_str", "bucket", "target", "commanded"),
        [
            ("fahrenheit", "7d", "_bucket_hourly", 66.0, 68.7),
            ("celsius", "7d", "_bucket_hourly", 18.9, 20.4),
            ("fahrenheit", "1y", "_bucket_daily", 66.0, 68.7),
            ("celsius", "1y", "_bucket_daily", 18.9, 20.4),
        ],
    )
    def test_long_range_chart_keeps_the_target_line_at_the_room_target(
        self, tmp_path, unit, range_str, bucket, target, commanded
    ):
        from tests.test_chart_celsius import _NOW, _daily_6day_forecast, _make_full_chart_coord, _run_chart

        log = self._log(tmp_path)
        buckets = getattr(log, bucket)(_bucket_rows(_NOW - timedelta(days=2)))
        coord = _make_full_chart_coord(daily_forecast_full=_daily_6day_forecast(), temp_unit=unit)
        coord._chart_log.get_entries.return_value = buckets
        result = _run_chart(coord, range_str)
        [eff] = [p["target"] for p in result["effective_target_history"]]
        [cmd] = [p["target"] for p in result["commanded_setpoint_history"]]
        assert eff == pytest.approx(target, abs=0.06)  # converted once (66 F -> 18.9 C), never twice or not at all
        assert cmd == pytest.approx(commanded, abs=0.06)
