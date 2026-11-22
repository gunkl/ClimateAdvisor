"""Issue #1678: night comfort incidents no longer fire on the bedroom loop's own heat-start reading (or at wake).

Occupant experience being guarded: overnight the bedroom drifts slowly to 64.2-64.4 F, the remote-sensor outer loop
(#1194) raises the hallway thermostat from 66 to 68, the furnace runs ~10 minutes and the bedroom is back near 68 F.
That is the planned sleep setback working, yet Climate Advisor logged a ``comfort_undertemp`` incident on nearly every
cycle (5-6 a night) and another one at wake-up, which the AI Investigator then reported as "the house was often colder
than the in-control claim suggests". The bedroom sensor reports in 0.1 C steps (0.18 F): with the loop triggering past
the MEASURED swing (1.44 F) and the detector tolerating the ROUNDED swing (1.5 F), no reading exists between 64.50 and
64.56 F, so every raise happened on a reading already past the detector's tolerance.

The real alarms stay (each has a negative test here): a loop at its cap, a gated loop (the occupant's override), a
stale bedroom sensor, the thermostat Off, a furnace that never starts, one that runs too long without recovering, and
a wake recovery that has not happened after 30 minutes or a thermostat CA never set to the new floor. The cool-mode
mirror (``comfort_violation`` above the sleep ceiling) is tested the same way. ``comfort_violations_minutes`` and the
stall alert share the gate (``_is_tolerated_comfort_deviation`` / ``_is_expected_control_deviation``) and are checked
through it. P7 (``comfort_kept``) judges the comfort target, not the loop-raised setpoint, and reports the real depth.

The quantised-night tests run the REAL coordinator + engine (the #1194 ``Night`` rig: real sleep-sensor listener,
real ``remote_offset_check``, real incident scan); the pure rules are tested directly at the end.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.climate_advisor.const import (
    COMFORT_SWING_TOLERANCE_MAX_F,
    REMOTE_CORRECTION_TOLERANCE_MAX_S,
    WAKE_STEP_GRACE_S,
)
from custom_components.climate_advisor.invariant_watchdog import PromiseConfig, run_promise_checks
from custom_components.climate_advisor.promise_journal import Reading, engine_comfort_target_f
from custom_components.climate_advisor.remote_offset import (
    RemoteLoopComfortView,
    loop_tolerates_deviation,
    sensor_step_from_values,
)
from custom_components.climate_advisor.swing import within_band_step_recovery
from tests.test_promise_checks import SCOPE_TEST, at, cmd, make_input
from tests.test_remote_offset_context_1194c import CtxNight
from tests.test_remote_offset_context_1194c import at as clock
from tests.test_remote_offset_gates_1194b import Night

# Zone 1's learned model: the loop triggers on the measured swing, the detectors tolerate the rounded-up one (#1477).
_ZONE1 = {"swing_heat_f": 1.5, "swing_heat_raw_f": 1.44, "swing_cool_f": 1.5, "swing_cool_raw_f": 1.44}


def _quantised(start_c: float, steps: int, sign: int) -> list[float]:
    """Readings of a 0.1 C sensor shown in F, ``steps`` steps from ``start_c`` (sign -1 = falling)."""
    return [round((start_c + sign * 0.1 * k) * 1.8 + 32.0, 2) for k in range(steps + 1)]


# heat: 19.0 C = 66.20 F down to 17.9 C = 64.22 F.  cool: 22.2 C = 71.96 F up to 23.3 C = 73.94 F.
_DRIFT = {"heat": _quantised(19.0, 11, -1), "cool": _quantised(22.2, 11, +1)}
_EDGE = {"heat": 66.0, "cool": 72.0}
_ACTION = {"heat": "heating", "cool": "cooling"}


def _with_zone1_swings(n: Night) -> Night:
    """Install Zone 1's measured/rounded swings where the engine (loop) and the coordinator (detectors) read them."""
    n.co._thermal_model = {**(getattr(n.co, "_thermal_model", None) or {}), **_ZONE1}  # what the engine reads
    real = n.co.learning.get_thermal_model

    def _model(*a, **k):
        return {**real(*a, **k), **_ZONE1}

    n.co.learning.get_thermal_model = _model
    assert n.ae._remote_swing_f(n.d["mode"]) == pytest.approx(1.44), "precondition: the loop's measured swing"
    return n


def _incidents(n: Night) -> list[dict]:
    return [e for e in n.co._event_log if e.get("type") == "incident_detected"]


def _scan(n: Night) -> list[str]:
    """Run the real incident scan now; return the comfort incident classes logged so far."""
    with n.sch.installed():
        n.co._detect_and_emit_incidents()
    return [
        e["incident_class"] for e in _incidents(n) if e["incident_class"] in ("comfort_undertemp", "comfort_violation")
    ]


def _tolerated(n: Night, indoor: float) -> bool:
    """The shared gate (incidents, comfort_violations_minutes, stall alert) on the sleep band."""
    with n.sch.installed():
        return n.co._is_tolerated_comfort_deviation(indoor, 66.0, 72.0)


def _drift_to_the_raise(n: Night, direction: str) -> float:
    """Report the quantised drift every 15 minutes (scanning as the coordinator would) until the loop raises."""
    n.ca_writes()  # CA wrote the sleep target; the thermostat holds it
    n.wait(150)
    for value in _DRIFT[direction]:
        n.set_bedroom(value)
        assert _scan(n) == [], f"an incident at {value} while the loop had not even acted yet"
        if n.sp != n.target:
            return value
        n.wait(15 * 60)
        assert _scan(n) == [], f"an incident at {value} during the slow drift"
    raise AssertionError(f"the loop never raised: {n.sp_writes()}")


# ---------------------------------------------------------------------------------------------------------------------
# The regression: a 0.18 F-step bedroom drifting through the trigger
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("direction", ["heat", "cool"])
class TestQuantisedNight:
    def test_no_incident_while_the_raise_is_starting_or_the_equipment_runs(self, direction: str) -> None:
        n = _with_zone1_swings(Night("hvac_fan", direction, bedroom_f=_DRIFT[direction][0]))
        trigger = _drift_to_the_raise(n, direction)
        edge = _EDGE[direction]
        gap = abs(edge - trigger)
        # The scenario is effective: the trigger reading is past the old (thermostat-swing) tolerance, so before the
        # fix this exact reading produced the incident.
        assert trigger == pytest.approx(64.4 if direction == "heat" else 73.6, abs=0.03)
        assert gap > 1.5 + 1e-9
        with n.sch.installed():
            assert not n.co._is_within_thermostat_hysteresis(trigger, 66.0, 72.0), "the old rule alone would fire"
        assert n.sp == n.target + n.d["sign"] * 2.0, "precondition: the loop raised one step"
        n.wait(5)  # inside the startup wait, the equipment has not reported yet
        assert _scan(n) == []
        n.thermostat_reports(hvac_action=_ACTION[direction])  # the thermostat starts
        n.wait(5 * 60)  # the reading is stale-but-fresh: the sensor reports only on change
        assert _scan(n) == []
        assert _tolerated(n, trigger), "violation minutes / stall alert must not count the loop's own cycle"
        # One step further out while the equipment runs: past swing + step, tolerated only as a correction.
        further = _DRIFT[direction][-1]
        n.set_bedroom(further)
        assert abs(edge - further) > 1.44 + 0.18
        assert _scan(n) == []
        assert _tolerated(n, further)

    def test_the_payload_names_the_sensor_the_tolerance_and_target_vs_commanded(self, direction: str) -> None:
        n = _with_zone1_swings(Night("hvac_fan", direction, bedroom_f=_DRIFT[direction][0]))
        _drift_to_the_raise(n, direction)
        n.wait(REMOTE_CORRECTION_TOLERANCE_MAX_S + 60)  # the equipment never started: past every allowance
        n.set_bedroom(_DRIFT[direction][-1] - n.d["sign"] * 1.0)  # and the room kept going: a real failure
        assert _scan(n), "a furnace that never brought the room back must still be an incident"
        p = _incidents(n)[-1]
        assert p["indoor_source_role"] == "sleep_sensor"
        assert isinstance(p["indoor_source_name"], str) and p["indoor_source_name"]
        assert p["tolerance_kind"] == "remote_loop"
        assert p["tolerance_f"] == pytest.approx(1.44 + 0.18, abs=0.02)
        assert p["comfort_target_f"] == pytest.approx(n.target)
        assert p["last_commanded_setpoint_f"] != pytest.approx(n.target), "the thermostat holds target + offset"


# ---------------------------------------------------------------------------------------------------------------------
# Real failures still fire (end to end)
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("direction", ["heat", "cool"])
class TestRealFailuresStillFire:
    def test_a_furnace_that_never_starts(self, direction: str) -> None:
        n = _with_zone1_swings(Night("hvac_fan", direction, bedroom_f=_DRIFT[direction][0]))
        _drift_to_the_raise(n, direction)
        further = _DRIFT[direction][-1]
        n.set_bedroom(further)  # inside the startup wait: the raise is still expected to start the equipment
        assert _scan(n) == []
        n.wait(10 * 60)  # well past the startup wait, nothing ever reported heating/cooling
        with n.sch.installed():
            view = n.ae.remote_loop_comfort_view()
        assert view is not None and not view.equipment_active, "the furnace never reported running"
        # (the loop stepped again after its wait; at the cap or past the wait, a furnace that never ran is a failure)
        n.set_bedroom(further - n.d["sign"] * 0.18)
        assert _scan(n) != []

    def test_the_loop_at_its_cap(self, direction: str) -> None:
        n = _with_zone1_swings(Night("hvac_fan", direction, bedroom_f=_DRIFT[direction][-1]))
        n.offset_at_cap()  # cold bedroom -> two raises -> +/-4 F
        n.thermostat_reports(hvac_action=_ACTION[direction])
        n.set_bedroom(_EDGE[direction] - n.d["sign"] * 2.0)  # 2.0 past the edge: beyond swing + step
        assert _scan(n) != [], "at the cap the loop has nothing left to give: a cold room is an incident"

    def test_a_gated_loop_a_window_opens(self, direction: str) -> None:
        n = _with_zone1_swings(Night("hvac_fan", direction, bedroom_f=_DRIFT[direction][0]))
        _drift_to_the_raise(n, direction)
        n.window(True)  # a monitored window opens: the loop freezes (inside the debounce), then the pause closes it
        n.thermostat_reports(hvac_action=_ACTION[direction])
        with n.sch.installed():
            assert n.ae.remote_loop_comfort_view() is None, "a gated loop has no view"
        n.set_bedroom(_DRIFT[direction][-1])
        assert _scan(n) != []

    def test_a_stale_bedroom_sensor(self, direction: str) -> None:
        n = _with_zone1_swings(Night("hvac_fan", direction, bedroom_f=_DRIFT[direction][0]))
        _drift_to_the_raise(n, direction)
        n.thermostat_reports(hvac_action=_ACTION[direction])
        n.set_bedroom(_DRIFT[direction][-1])
        n.wait(121 * 60)  # no report for over two hours: the loop drops the offset, the reading is stale
        with n.sch.installed():
            assert n.ae.remote_loop_comfort_view() is None, "a stale remote reading has no view"
        assert _scan(n) != []

    def test_the_thermostat_off(self, direction: str) -> None:
        n = _with_zone1_swings(Night("hvac_fan", direction, bedroom_f=_DRIFT[direction][0]))
        _drift_to_the_raise(n, direction)
        n.thermostat_reports(state="off", hvac_action="off")
        n.wait(30)
        assert _scan(n) != [], "nothing holds the room with the thermostat Off"


# ---------------------------------------------------------------------------------------------------------------------
# P7 on the same night: the comfort target, not the raised setpoint; the real depth in the message
# ---------------------------------------------------------------------------------------------------------------------
class TestP7OnTheLoopNight:
    @staticmethod
    def _night(*, target: float | None) -> list[Reading]:
        """60 min idle in the deadband at 66 (bedroom 64.9 -> 64.4), then the loop's raise to 68 (still idle)."""
        rs = [
            Reading(
                ts=at(m),
                hvac_mode="heat",
                hvac_action="idle",
                indoor_f=64.94 if m < 30 else 64.4,
                setpoint_f=66.0,
                forecast_high_f=50.0,
            )
            for m in range(0, 60, 5)
        ]
        rs.append(
            Reading(ts=at(60), hvac_mode="heat", hvac_action="idle", indoor_f=64.4, setpoint_f=68.0,
                    comfort_target_f=target, forecast_high_f=50.0)
        )  # fmt: skip
        return rs

    @staticmethod
    def _hits(rs: list[Reading]) -> list:
        cfg = PromiseConfig.from_config({"comfort_heat": 66.0, "comfort_cool": 72.0})
        # The setpoint changes are Climate Advisor's own writes (the loop's raise), never an occupant episode.
        writes = [cmd(m, service="set_temperature", mode="heat", setpoint_f=sp) for m, sp in ((-1, 66.0), (59.9, 68.0))]
        return [
            h for h in run_promise_checks(make_input(rs, writes), cfg, scope=SCOPE_TEST) if h.promise == "comfort_kept"
        ]

    def test_the_raise_reading_is_judged_against_the_comfort_target(self) -> None:
        assert self._hits(self._night(target=66.0)) == []

    def test_without_the_target_the_raise_was_the_false_miss(self) -> None:
        """Negative control: the pre-fix reading (no target) reproduces the 60-minute miss of Nov 20, 22:41."""
        (h,) = self._hits(self._night(target=None))
        ev = dict(h.evidence)
        assert ev["worst_f"] == pytest.approx(0.6)  # past P7's own 1 F slack (what the log said)
        assert ev["depth_f"] == pytest.approx(1.6)  # past the band edge
        assert "1.6°F outside your comfort range" in h.detail
        assert "0.6°F" not in h.detail

    def test_a_target_far_above_the_room_is_still_not_the_deadband(self) -> None:
        """The excuse is the thermostat's own deadband: a target 3 F above the room is not (owner rule, #1260)."""
        rs = [
            Reading(ts=at(m), hvac_mode="heat", hvac_action="idle", indoor_f=64.4, setpoint_f=70.0,
                    comfort_target_f=67.0, forecast_high_f=50.0)
            for m in range(0, 61, 5)
        ]  # fmt: skip
        assert len(self._hits(rs)) == 1


def test_production_promise_reading_carries_the_target_while_the_loop_holds_an_offset() -> None:
    n = _with_zone1_swings(Night("hvac_fan", "heat", bedroom_f=_DRIFT["heat"][0]))
    with n.sch.installed():
        assert n.co._promise_reading_now(n.sch.now()).comfort_target_f is None, "no offset yet: no target"
    _drift_to_the_raise(n, "heat")
    with n.sch.installed():
        r = n.co._promise_reading_now(n.sch.now())
    assert r.setpoint_f == pytest.approx(68.0) and r.comfort_target_f == pytest.approx(66.0)
    assert engine_comfort_target_f(n.ae) == pytest.approx(66.0)  # the one reader the sim adapter shares


def test_engine_comfort_target_reader_degrades_to_none() -> None:
    class Broken:
        def remote_comfort_target_f(self):
            raise RuntimeError("boom")

    assert engine_comfort_target_f(None) is None
    assert engine_comfort_target_f(Broken()) is None
    assert engine_comfort_target_f(type("E", (), {"remote_comfort_target_f": lambda self: True})()) is None


# ---------------------------------------------------------------------------------------------------------------------
# Wake: a bounded grace after the band steps toward the room
# ---------------------------------------------------------------------------------------------------------------------
_WAKE = {"comfort_heat": 68.0, "comfort_cool": 75.0, "sleep_heat": 66.0, "sleep_cool": 72.0}


def _wake_night() -> CtxNight:
    n = CtxNight(clock(6, 20), "hvac_fan", "heat", bedroom_f=66.2, config=dict(_WAKE))
    n.ca_writes()
    n.thermostat_reports(current_temperature=66.0)  # the hallway sits at the sleep floor
    n.run_to(6, 30, 0)  # wake: the band steps to [68, 75] and the wake handler sets the thermostat to 68
    assert n.sp == pytest.approx(68.0), f"precondition: the wake write landed: {n.sp_writes()}"
    return n


def _day_tolerated(n: CtxNight, indoor: float) -> bool:
    with n.sch.installed():
        return n.co._is_tolerated_comfort_deviation(indoor, 68.0, 75.0)


class TestWakeGrace:
    def test_no_incident_at_the_wake_instant_even_before_the_furnace_reports(self) -> None:
        n = _wake_night()
        assert n.fh.states.get(n.ce).attributes.get("hvac_action") == "idle", "precondition: not heating yet"
        assert _scan(n) == []
        assert _day_tolerated(n, 66.0)

    def test_after_the_grace_the_unrecovered_house_is_an_incident(self) -> None:
        n = _wake_night()
        n.run_to(7, 0, 30)  # past WAKE_STEP_GRACE_S
        assert not _day_tolerated(n, 66.0)
        assert _scan(n) == ["comfort_undertemp"]

    def test_a_thermostat_not_set_to_the_new_floor_gets_no_grace(self) -> None:
        n = _wake_night()
        n.wait(150)
        n.occupant_dials(66.0)  # the thermostat stays at the sleep floor: nothing is warming the house
        assert _scan(n) == ["comfort_undertemp"]

    def test_a_room_further_than_the_step_plus_swing_is_an_incident(self) -> None:
        n = _wake_night()
        assert not _day_tolerated(n, 64.4)  # 3.6 below 68: more than the 2 F step + 1.5 swing


# ---------------------------------------------------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------------------------------------------------
def _view(**kw) -> RemoteLoopComfortView:
    base = {
        "mode": "heat",
        "remote_value_f": 64.4,
        "swing_f": 1.44,
        "sensor_step_f": 0.18,
        "offset_f": 2.0,
        "cap_f": 4.0,
        "seconds_since_raise": 60.0,
        "wait_s": 120.0,
        "equipment_active": False,
    }
    base.update(kw)
    return RemoteLoopComfortView(**base)


class TestLoopRule:
    def test_swing_plus_one_step_is_the_loops_own_band(self) -> None:
        assert loop_tolerates_deviation(64.4, 66.0, 72.0, "heat", _view(offset_f=0.0, seconds_since_raise=None))
        assert not loop_tolerates_deviation(
            64.22, 66.0, 72.0, "heat", _view(remote_value_f=64.22, offset_f=0.0, seconds_since_raise=None)
        )

    def test_unknown_sensor_step_adds_nothing(self) -> None:
        v = _view(sensor_step_f=0.0, offset_f=0.0, seconds_since_raise=None)
        assert not loop_tolerates_deviation(64.4, 66.0, 72.0, "heat", v)

    def test_correcting_inside_the_wait_or_while_running(self) -> None:
        v = _view(remote_value_f=64.22)
        assert loop_tolerates_deviation(64.22, 66.0, 72.0, "heat", v)  # inside the wait
        assert not loop_tolerates_deviation(64.22, 66.0, 72.0, "heat", v._replace(seconds_since_raise=600.0))
        assert loop_tolerates_deviation(
            64.22, 66.0, 72.0, "heat", v._replace(seconds_since_raise=600.0, equipment_active=True)
        )

    def test_running_too_long_is_a_failure(self) -> None:
        v = _view(
            remote_value_f=64.22, equipment_active=True, seconds_since_raise=REMOTE_CORRECTION_TOLERANCE_MAX_S + 1
        )
        assert not loop_tolerates_deviation(64.22, 66.0, 72.0, "heat", v)

    def test_at_the_cap_only_the_loops_own_band_is_tolerated(self) -> None:
        v = _view(remote_value_f=64.22, offset_f=4.0, equipment_active=True)
        assert not loop_tolerates_deviation(64.22, 66.0, 72.0, "heat", v)
        assert loop_tolerates_deviation(64.4, 66.0, 72.0, "heat", v._replace(remote_value_f=64.4))

    def test_never_beyond_the_owner_cap(self) -> None:
        cold = 66.0 - COMFORT_SWING_TOLERANCE_MAX_F - 0.1
        v = _view(remote_value_f=cold, swing_f=3.0, equipment_active=True)
        assert not loop_tolerates_deviation(cold, 66.0, 72.0, "heat", v)

    @pytest.mark.parametrize("mode", ["off", "cool", "heat_cool", None])
    def test_only_while_the_thermostat_is_in_the_loops_mode(self, mode) -> None:
        assert not loop_tolerates_deviation(64.4, 66.0, 72.0, mode, _view(equipment_active=True))

    def test_no_view_or_another_reading(self) -> None:
        assert not loop_tolerates_deviation(64.4, 66.0, 72.0, "heat", None)
        assert not loop_tolerates_deviation(64.4, 66.0, 72.0, "heat", _view(remote_value_f=65.0, equipment_active=True))
        assert not loop_tolerates_deviation(64.4, 66.0, 72.0, "heat", _view(mode="fan"))

    def test_in_band_and_the_wrong_side_are_not_this_rule(self) -> None:
        assert not loop_tolerates_deviation(66.5, 66.0, 72.0, "heat", _view(remote_value_f=66.5))
        assert not loop_tolerates_deviation(64.4, None, 72.0, "heat", _view())
        assert not loop_tolerates_deviation(71.0, 66.0, 72.0, "cool", _view(mode="cool", remote_value_f=71.0))
        assert not loop_tolerates_deviation(73.6, 66.0, None, "cool", _view(mode="cool", remote_value_f=73.6))

    def test_cool_mirror(self) -> None:
        v = _view(mode="cool", remote_value_f=73.78, equipment_active=True)
        assert loop_tolerates_deviation(73.78, 66.0, 72.0, "cool", v)
        assert not loop_tolerates_deviation(73.78, 66.0, 72.0, "cool", v._replace(offset_f=4.0))


class TestSensorStep:
    def test_tenth_of_a_celsius(self) -> None:
        assert sensor_step_from_values(_DRIFT["heat"]) == pytest.approx(0.18, abs=0.011)

    def test_whole_degree_and_bounds(self) -> None:
        assert sensor_step_from_values([64.0, 65.0, 66.0]) == 1.0
        assert sensor_step_from_values([64.0, 64.01, 64.02]) == 0.05
        assert sensor_step_from_values([60.0, 64.0, 70.0]) == 1.8

    def test_too_few_or_junk(self) -> None:
        assert sensor_step_from_values([64.4, 64.58]) == 0.0
        assert sensor_step_from_values(None) == 0.0
        assert sensor_step_from_values([64.4, "x", float("nan"), 64.58]) == 0.0


class TestBandStepRule:
    @staticmethod
    def _ok(indoor, **kw) -> bool:
        base = {
            "prev_floor_f": 66.0,
            "prev_ceiling_f": 72.0,
            "seconds_since_step": 60.0,
            "max_s": WAKE_STEP_GRACE_S,
            "thermostat_mode": "heat",
            "setpoint_f": 68.0,
            "swing_heat_f": 1.5,
            "swing_cool_f": 1.5,
        }
        base.update(kw)
        return within_band_step_recovery(indoor, 68.0, 75.0, **base)

    def test_inside_the_step_plus_swing(self) -> None:
        assert self._ok(66.0)
        assert self._ok(64.5)
        assert not self._ok(64.4)

    def test_bounded_in_time(self) -> None:
        assert not self._ok(66.0, seconds_since_step=WAKE_STEP_GRACE_S)
        assert not self._ok(66.0, seconds_since_step=None)
        assert not self._ok(66.0, seconds_since_step=-1.0)

    def test_needs_a_step_toward_the_room_a_holding_mode_and_the_new_setpoint(self) -> None:
        assert not self._ok(66.0, prev_floor_f=68.0)
        assert not self._ok(66.0, thermostat_mode="off")
        assert not self._ok(66.0, setpoint_f=66.0)
        assert not self._ok(66.0, setpoint_f=None)

    def test_cool_mirror(self) -> None:
        def ok(indoor, **kw):
            base = {
                "prev_floor_f": 68.0,
                "prev_ceiling_f": 78.0,
                "seconds_since_step": 60.0,
                "max_s": WAKE_STEP_GRACE_S,
                "thermostat_mode": "cool",
                "setpoint_f": 75.0,
                "swing_heat_f": 1.5,
                "swing_cool_f": 1.5,
            }
            base.update(kw)
            return within_band_step_recovery(indoor, 68.0, 75.0, **base)

        assert ok(77.0)
        assert not ok(79.6)
        assert not ok(77.0, setpoint_f=78.0)
        assert not ok(77.0, prev_ceiling_f=75.0)
        assert not ok(77.0, thermostat_mode="heat")

    def test_in_band_is_not_this_rule(self) -> None:
        assert not self._ok(70.0)


def test_the_wake_window_helper_bounds(monkeypatch) -> None:
    n = _wake_night()
    with n.sch.installed():
        step = n.ae.wake_band_step()
        assert step is not None
        before, after, since = step
        assert (before.floor, after.floor) == (66.0, 68.0) and 0.0 <= since < WAKE_STEP_GRACE_S
        assert n.ae.wake_band_step(n.sch.now() + timedelta(seconds=WAKE_STEP_GRACE_S)) is None
        assert n.ae.wake_band_step(n.sch.now() - timedelta(minutes=5)) is None
        n.ae.config = {**n.ae.config, "wake_time": "bad"}
        assert n.ae.wake_band_step() is None
        n.ae.config = {**n.ae.config, "wake_time": None}
        assert n.ae.wake_band_step() is None


# ---------------------------------------------------------------------------------------------------------------------
# The shared gate: comfort_violations_minutes and the stall alert change with it
# ---------------------------------------------------------------------------------------------------------------------
def _minutes_after(n: Night, indoor: float) -> float:
    """Comfort Score minutes the real tracker adds for ``indoor`` over 30 minutes of the sleep band."""
    from custom_components.climate_advisor.learning import DailyRecord  # noqa: PLC0415

    if n.co._today_record is None:  # the rig's record can be rolled over by the night's date change
        n.co._today_record = DailyRecord(date="2026-11-03", day_type="cold", trend_direction="stable")
    n.co._last_violation_check = None  # count a full 30-minute slice
    # The rig has no hourly forecast, so the chart band (what the score judges) is unresolved and would fall back to
    # the day range; pin the sleep band the engine holds tonight, as production resolves it.
    n.co._comfort_score_band_now = lambda: (66.0, 72.0)
    before = n.co._today_record.comfort_violations_minutes
    with n.sch.installed():
        n.co._track_indoor_comfort(indoor)
    return n.co._today_record.comfort_violations_minutes - before


def _stall_inputs(n: Night, indoor: float, monkeypatch) -> dict:
    import custom_components.climate_advisor.coordinator as coord_mod  # noqa: PLC0415

    seen: dict = {}
    real = coord_mod.decide_comfort_stall

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(coord_mod, "decide_comfort_stall", spy)
    with n.sch.installed():
        n.co._update_comfort_stall(indoor, 66.0, 72.0, n.sch.now())
    return seen


@pytest.mark.parametrize("direction", ["heat", "cool"])
def test_violation_minutes_and_stall_follow_the_gate(direction: str, monkeypatch) -> None:
    n = _with_zone1_swings(Night("hvac_fan", direction, bedroom_f=_DRIFT[direction][0]))
    trigger = _drift_to_the_raise(n, direction)
    n.thermostat_reports(hvac_action=_ACTION[direction])
    assert _minutes_after(n, trigger) == 0.0, "the loop's own cycle is not a violation minute"
    assert _stall_inputs(n, trigger, monkeypatch)["thermostat_swing_tolerated"] is True
    # The real failure: the thermostat Off with the same room -> minutes count and the stall clock is not excused.
    n.thermostat_reports(state="off", hvac_action="off")
    assert not _tolerated(n, trigger)
    assert _minutes_after(n, trigger) == pytest.approx(30.0)
    assert _stall_inputs(n, trigger, monkeypatch)["thermostat_swing_tolerated"] is False


# ---------------------------------------------------------------------------------------------------------------------
# The Activity row: one decimal, the sensor, the tolerance, target vs commanded
# ---------------------------------------------------------------------------------------------------------------------
_ROW = {
    "incident_class": "comfort_undertemp",
    "incident_id": "2026-11-21T01:34:19",
    "indoor_f": 64.4,
    "comfort_heat": 66.0,
    "comfort_cool": 72.0,
    "occupancy_mode": "home",
    "indoor_source_name": "Primary Bedroom Sensor Temperature",
    "indoor_source_role": "sleep_sensor",
    "tolerance_f": 1.62,
    "tolerance_kind": "remote_loop",
    "thermostat_mode": "heat",
    "thermostat_action": "idle",
    "last_commanded_mode": "heat",
    "last_commanded_setpoint_f": 68.0,
    "last_command_result": "sent",
    "comfort_target_f": 66.0,
}


def _row(payload: dict, unit: str = "fahrenheit") -> str:
    import custom_components.climate_advisor.ai_skills_context as act  # noqa: PLC0415
    from custom_components.climate_advisor import i18n  # noqa: PLC0415

    _label, settings = act.EVENT_RENDERERS["incident_detected"](payload, unit)
    return settings if isinstance(settings, str) else i18n.render_en(settings)


def test_the_incident_row_says_what_was_judged() -> None:
    text = _row(_ROW)
    assert "indoor 64.4°F vs. band [66°F/72°F]" in text
    assert "read from Primary Bedroom Sensor Temperature (sleep sensor)" in text
    assert "tolerance 1.6°F (remote-sensor loop swing plus one sensor step)" in text
    assert "CA last tried: heat @ 68°F (sent)" in text
    assert "target 66°F, thermostat set to 68°F" in text


def test_the_incident_row_without_the_new_fields_is_unchanged_but_one_decimal() -> None:
    old = {k: v for k, v in _ROW.items() if k not in ("indoor_source_name", "tolerance_f", "comfort_target_f")}
    text = _row(old)
    assert "64.4°F" in text and "read from" not in text and "tolerance" not in text and "target " not in text


@pytest.mark.parametrize(
    ("kind", "words"),
    [("thermostat_swing", "(thermostat swing)"), ("fixed", "(fixed, the thermostat mode does not hold this side)")],
)
def test_the_incident_row_names_each_tolerance(kind: str, words: str) -> None:
    assert words in _row({**_ROW, "tolerance_kind": kind, "tolerance_f": 1.5})


def test_the_incident_row_hides_target_when_it_equals_the_setpoint_and_converts_units() -> None:
    assert "thermostat set to" not in _row({**_ROW, "comfort_target_f": 68.0})
    text = _row(_ROW, unit="celsius")
    assert "18.0°C" in text and "64.4" not in text


# ---------------------------------------------------------------------------------------------------------------------
# Degradation: a comfort-gate read never breaks the update cycle
# ---------------------------------------------------------------------------------------------------------------------
class TestReadsDegrade:
    def test_engine_reads(self, monkeypatch) -> None:
        n = _with_zone1_swings(Night("hvac_fan", "heat", bedroom_f=_DRIFT["heat"][0]))
        before = list(n.ae._remote_seen_values)
        for junk in ("x", True, float("nan"), None):
            n.ae._note_remote_reading(junk)
        assert n.ae._remote_seen_values == before
        with n.sch.installed():
            monkeypatch.setattr(n.ae, "_remote_context", lambda: None)
            assert n.ae.remote_loop_comfort_view() is None  # no remote reading in use
            monkeypatch.setattr(n.ae, "_remote_context", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
            assert n.ae.remote_loop_comfort_view() is None
            monkeypatch.setattr(n.ae, "_remote_offset_in_play", lambda: False)
            assert n.ae.remote_loop_comfort_view() is None

    def test_wake_band_resolver_fault(self, monkeypatch) -> None:
        import custom_components.climate_advisor.automation as auto  # noqa: PLC0415

        n = _wake_night()

        def boom(*a, **k):
            raise RuntimeError("resolver")

        monkeypatch.setattr(auto, "resolve_active_band", boom)
        with n.sch.installed():
            assert n.ae.wake_band_step() is None
        n.ae._current_classification = None
        with n.sch.installed():
            assert n.ae.wake_band_step() is None

    def test_coordinator_reads(self, monkeypatch) -> None:
        n = _with_zone1_swings(Night("hvac_fan", "heat", bedroom_f=_DRIFT["heat"][0]))
        co, ae = n.co, n.ae

        def boom(*a, **k):
            raise RuntimeError("boom")

        with n.sch.installed():
            monkeypatch.setattr(ae, "remote_loop_comfort_view", boom)
            monkeypatch.setattr(ae, "wake_band_step", boom)
            assert co._remote_loop_view() is None
            assert co._is_wake_step_recovery(64.0, 66.0, 72.0) is False
            monkeypatch.setattr(ae, "wake_band_step", lambda: ("not", "a", "band"))
            assert co._is_wake_step_recovery(64.0, 66.0, 72.0) is False
            monkeypatch.setattr(co, "_get_indoor_temp_with_provenance", boom)
            assert "indoor_source_name" not in co._indoor_incident_context()
            monkeypatch.setattr(co.hass.states, "get", lambda _e: None)
            assert co._thermostat_setpoint_f() is None
            monkeypatch.setattr(co, "automation_engine", None)
            assert co._remote_loop_view() is None
            assert co._is_wake_step_recovery(64.0, 66.0, 72.0) is False
            assert co._thermostat_setpoint_f() is None
            assert co._indoor_incident_context() == {}
