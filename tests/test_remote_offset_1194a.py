"""Issue #1194 increment (a): the remote-sensor outer loop's offset core.

Occupant outcome: on a night when the bedroom sits below the sleep target while the hallway
thermostat reads at its setpoint (and so never starts the furnace), Climate Advisor nudges the
thermostat's setpoint up in small steps until the thermostat fires, then steps it back down once
the BEDROOM is warm enough, so the person in the bedroom gets their sleep temperature without CA
fighting the thermostat's own hysteresis or cycling the equipment extra.

These tests drive the REAL ``AutomationEngine`` through the headless harness (FakeHass +
FakeScheduler virtual clock) and assert on the thermostat service calls it actually makes.

Rule under test (heat; cool mirrors):
  * raise one step when remote < target - swing   (after the startup wait / actuation feedback),
  * step down one step when remote > target + swing,
  * hold in between; at most one step per check; write only when the commanded value changes.
Caps (tighter wins): <= +4F / +2C beyond target, never past the OPPOSITE comfort-band edge.
Freshness: remote reading older than 120 min (``last_reported``) -> plain setpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor.automation import ComfortBand
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.temperature import from_fahrenheit, to_fahrenheit
from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_engine import build_headless_engine
from tools.sim_harness.fake_hass import FakeState

ENTITY = "climate.test_thermostat"
BEDROOM = "sensor.bedroom_temp"
NIGHT = datetime(2026, 11, 2, 23, 0, 0, tzinfo=UTC)  # inside 20:30 -> 06:30
DAY = datetime(2026, 11, 2, 14, 0, 0, tzinfo=UTC)  # outside the sleep window

BASE_CONFIG = {
    "sleep_time": "20:30",
    "wake_time": "06:30",
    "sleep_heat": 66.0,
    "sleep_cool": 72.0,
    "comfort_heat": 70.0,
    "comfort_cool": 75.0,
    "sleep_indoor_temp_entity": BEDROOM,
    "fan_mode": "disabled",
    "thermostat_hvac_modes": ["off", "heat", "cool"],
}


def _classification(hvac_mode: str = "heat") -> DayClassification:
    c = object.__new__(DayClassification)
    c.__dict__.update(
        {
            "day_type": "mild",
            "trend_direction": "stable",
            "trend_magnitude": 0,
            "today_high": 60,
            "today_low": 40,
            "tomorrow_high": 60,
            "tomorrow_low": 40,
            "hvac_mode": hvac_mode,
            "pre_condition": False,
            "pre_condition_target": None,
            "windows_recommended": False,
            "window_open_time": None,
            "window_close_time": None,
            "setback_modifier": 0.0,
            "window_opportunity_morning": False,
            "window_opportunity_evening": False,
        }
    )
    return c


def _ro():
    """Import the pure module; a missing module is an assertion failure (feature absent), not a skip."""
    import importlib

    try:
        return importlib.import_module("custom_components.climate_advisor.remote_offset")
    except ModuleNotFoundError:
        raise AssertionError("remote_offset module is absent (feature not built)") from None


class Env:
    """A real engine + fake thermostat + fake bedroom sensor, in either unit."""

    def __init__(self, *, mode="heat", unit="fahrenheit", start=NIGHT, config=None, hall_f=67.0, bedroom_f=64.0):
        self.unit = unit
        cfg = dict(BASE_CONFIG)
        cfg["temp_unit"] = unit
        if config:
            cfg.update(config)
        self.engine, self.hass, self.sched, self.log = build_headless_engine(
            config=cfg,
            climate_entity=ENTITY,
            climate_state=mode,
            climate_attributes={
                "current_temperature": self._disp(hall_f),
                "temperature": self._disp(66.0),
                "hvac_action": "idle",
            },
            start_time=start,
        )
        self.engine._current_classification = _classification(mode)
        self.set_bedroom(bedroom_f)

    # -- unit helpers -----------------------------------------------------
    def _disp(self, f: float) -> float:
        return round(from_fahrenheit(f, self.unit), 4)

    # -- world ------------------------------------------------------------
    def set_bedroom(self, f: float, *, reported: datetime | None = None) -> None:
        stamp = reported if reported is not None else self.sched.now()
        self.hass.states.set(
            BEDROOM,
            FakeState(
                state=str(self._disp(f)),
                attributes={"unit_of_measurement": "°C" if self.unit == "celsius" else "°F"},
                last_changed=stamp,
                last_updated=stamp,
                last_reported=stamp,
            ),
        )

    def set_action(self, action: str) -> None:
        cs = self.hass.states.get(ENTITY)
        attrs = dict(cs.attributes)
        attrs["hvac_action"] = action
        self.hass.states.set(ENTITY, FakeState(state=cs.state, attributes=attrs))

    def advance(self, seconds: float) -> None:
        with self.sched.installed():
            self.sched.advance_by(seconds)

    def run(self, coro):
        with self.sched.installed():
            return run_coro(coro)

    # -- actions ----------------------------------------------------------
    def write(self, target_f: float, *, mode="heat", reason="bedtime"):
        return self.run(self.engine._set_temperature(target_f, reason=reason, mode=mode))

    def check(self):
        return self.run(self.engine.remote_offset_check())

    # -- observations -----------------------------------------------------
    def sp_calls(self) -> list[float]:
        """Every set_temperature service value sent, in display unit, in order."""
        return [
            a["data"]["temperature"]
            for a in self.hass.action_log
            if a["domain"] == "climate" and a["service"] == "set_temperature"
        ]

    def sp_f(self) -> list[float]:
        return [round(to_fahrenheit(v, self.unit), 2) for v in self.sp_calls()]

    def last_f(self) -> float:
        return self.sp_f()[-1]


# ---------------------------------------------------------------------------
# Pure decision core
# ---------------------------------------------------------------------------


class TestDecisionCore:
    def _decide(self, **kw):
        decide_remote_offset = _ro().decide_remote_offset

        args = {
            "mode": "heat",
            "target_f": 66.0,
            "remote_f": 64.0,
            "swing_f": 1.5,
            "magnitude_f": 0.0,
            "step_f": 2.0,
            "max_magnitude_f": 4.0,
            "seconds_since_raise": None,
            "wait_s": 300.0,
            "equipment_active": False,
        }
        args.update(kw)
        return decide_remote_offset(**args)

    def test_raise_below_target_minus_swing(self):
        d = self._decide(remote_f=64.4)  # gap 1.6 > 1.5
        assert d.action.value == "raise" and d.magnitude_f == 2.0

    def test_hold_inside_band_both_edges(self):
        assert self._decide(remote_f=64.5).action.value == "hold"  # gap exactly 1.5
        assert self._decide(remote_f=67.5, magnitude_f=2.0).action.value == "hold"  # exactly target+swing

    def test_step_down_above_target_plus_swing(self):
        d = self._decide(remote_f=67.6, magnitude_f=4.0)
        assert d.action.value == "step_down" and d.magnitude_f == 2.0

    def test_step_down_never_below_zero(self):
        d = self._decide(remote_f=70.0, magnitude_f=0.0)
        assert d.action.value == "hold" and d.magnitude_f == 0.0

    def test_one_step_only(self):
        assert self._decide(remote_f=50.0).magnitude_f == 2.0
        assert self._decide(remote_f=80.0, magnitude_f=4.0).magnitude_f == 2.0

    def test_cap_stops_raise(self):
        d = self._decide(remote_f=50.0, magnitude_f=4.0)
        assert d.action.value == "hold" and d.magnitude_f == 4.0 and d.reason == "at_cap"

    def test_cap_clips_partial_step(self):
        d = self._decide(remote_f=50.0, magnitude_f=2.0, max_magnitude_f=3.0)
        assert d.magnitude_f == 3.0

    def test_state_clipped_when_cap_shrinks(self):
        d = self._decide(remote_f=66.0, magnitude_f=4.0, max_magnitude_f=1.0)
        assert d.magnitude_f == 1.0

    def test_wait_blocks_second_raise(self):
        d = self._decide(remote_f=60.0, magnitude_f=2.0, seconds_since_raise=60.0)
        assert d.action.value == "hold" and d.reason == "awaiting_startup"
        d = self._decide(remote_f=60.0, magnitude_f=2.0, seconds_since_raise=301.0)
        assert d.action.value == "raise"

    def test_running_equipment_blocks_raise_but_not_step_down(self):
        assert self._decide(remote_f=60.0, equipment_active=True).action.value == "hold"
        d = self._decide(remote_f=70.0, magnitude_f=4.0, equipment_active=True)
        assert d.action.value == "step_down"

    def test_wait_does_not_block_step_down(self):
        d = self._decide(remote_f=70.0, magnitude_f=2.0, seconds_since_raise=5.0)
        assert d.action.value == "step_down"

    def test_cool_mirror(self):
        d = self._decide(mode="cool", target_f=72.0, remote_f=73.6)  # gap 1.6
        assert d.action.value == "raise" and d.magnitude_f == 2.0
        d = self._decide(mode="cool", target_f=72.0, remote_f=70.4, magnitude_f=4.0)  # gap -1.6
        assert d.action.value == "step_down" and d.magnitude_f == 2.0
        assert self._decide(mode="cool", target_f=72.0, remote_f=70.0).action.value == "hold"  # cold in cool: no-op


class TestUnitsAndWait:
    def test_step_and_cap_in_internal_fahrenheit(self):
        cap_f, step_f = _ro().cap_f, _ro().step_f

        assert step_f("fahrenheit") == 2.0 and cap_f("fahrenheit") == 4.0
        assert step_f("celsius") == pytest.approx(1.8) and cap_f("celsius") == pytest.approx(3.6)

    def test_unknown_unit_is_fahrenheit(self):
        cap_f, step_f = _ro().cap_f, _ro().step_f

        assert step_f("kelvin") == 2.0 and cap_f("kelvin") == 4.0

    def test_startup_wait_default_and_measured(self):
        startup_wait_s = _ro().startup_wait_s

        assert startup_wait_s(None) == 300.0  # unmeasured home
        assert startup_wait_s(5.0) == 120.0  # measured 2-5 s latency -> 2 min
        assert startup_wait_s(10_000.0) == 300.0  # bounded by the default

    def test_max_magnitude_tighter_of_cap_and_opposite_edge(self):
        max_magnitude_f = _ro().max_magnitude_f

        assert max_magnitude_f("heat", 66.0, floor=66.0, ceiling=72.0, cap_f=4.0) == 4.0  # +4 binds
        assert max_magnitude_f("heat", 66.0, floor=66.0, ceiling=68.0, cap_f=4.0) == 2.0  # ceiling binds
        assert max_magnitude_f("cool", 72.0, floor=66.0, ceiling=72.0, cap_f=4.0) == 4.0
        assert max_magnitude_f("cool", 72.0, floor=70.0, ceiling=72.0, cap_f=4.0) == 2.0
        assert max_magnitude_f("heat", 75.0, floor=66.0, ceiling=72.0, cap_f=4.0) == 0.0  # target past edge


class TestProvenance:
    def test_swapped_reading_carries_remote_context(self):
        from custom_components.climate_advisor.indoor_temp import resolve_indoor_temp_with_provenance

        env = Env()
        reading = resolve_indoor_temp_with_provenance(
            hass=env.hass,
            source="climate_fallback",
            unit="fahrenheit",
            indoor_temp_entity=None,
            climate_entity=ENTITY,
            in_sleep_window=True,
            sleep_indoor_temp_entity=BEDROOM,
        )
        assert reading.remote_context == "sleep" and reading.value == 64.0 and reading.primary_value == 67.0

    def test_primary_reading_has_no_remote_context(self):
        from custom_components.climate_advisor.indoor_temp import resolve_indoor_temp_with_provenance

        env = Env()
        reading = resolve_indoor_temp_with_provenance(
            hass=env.hass,
            source="climate_fallback",
            unit="fahrenheit",
            indoor_temp_entity=None,
            climate_entity=ENTITY,
            in_sleep_window=False,
            sleep_indoor_temp_entity=BEDROOM,
        )
        assert reading.remote_context is None


# ---------------------------------------------------------------------------
# Engine behaviour: heat
# ---------------------------------------------------------------------------


class TestHeatLoop:
    def test_raises_one_step_when_bedroom_below_target_minus_swing(self):
        env = Env(bedroom_f=64.0)  # 66 - 1.5 = 64.5 -> 64.0 is below
        env.write(66.0)
        assert env.sp_f() == [66.0]
        env.check()
        assert env.sp_f() == [66.0, 68.0]

    def test_holds_inside_band_and_writes_nothing(self):
        env = Env(bedroom_f=65.0)
        env.write(66.0)
        env.check()
        env.check()
        assert env.sp_f() == [66.0]

    def test_at_most_one_step_per_check(self):
        env = Env(bedroom_f=58.0)  # enormous gap
        env.write(66.0)
        env.check()
        assert env.sp_f() == [66.0, 68.0]  # +2 only, never straight to the cap

    def test_steps_down_when_bedroom_above_target_plus_swing(self):
        env = Env(bedroom_f=64.0)
        env.write(66.0)
        env.check()  # -> 68
        env.advance(400)
        env.set_bedroom(63.0)
        env.check()  # -> 70
        assert env.last_f() == 70.0
        env.set_bedroom(68.0)  # 66 + 1.5 = 67.5 -> above
        env.check()
        assert env.last_f() == 68.0
        env.check()
        assert env.last_f() == 66.0  # one step per check, back to the plain setpoint
        n = len(env.sp_f())
        env.check()
        assert len(env.sp_f()) == n  # nothing left to lower: no write

    def test_cap_plus_four(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        for _ in range(5):
            env.check()
            env.advance(400)
            env.set_bedroom(58.0)
        assert max(env.sp_f()) == 70.0  # 66 + 4, never 72

    def test_opposite_band_edge_cap(self):
        env = Env(bedroom_f=58.0, config={"sleep_cool": 67.0})  # ceiling 67 -> room for +1 only
        env.write(66.0)
        for _ in range(3):
            env.check()
            env.advance(400)
            env.set_bedroom(58.0)
        assert max(env.sp_f()) == 67.0

    def test_no_write_when_step_unchanged_at_cap(self):
        env = Env(bedroom_f=58.0, config={"sleep_cool": 67.0})
        env.write(66.0)
        env.check()
        n = len(env.sp_f())
        for _ in range(3):
            env.advance(400)
            env.set_bedroom(58.0)
            env.check()
        assert len(env.sp_f()) == n


class TestSwingSource:
    def test_learned_swing_sets_hysteresis(self):
        env = Env(bedroom_f=65.3)  # gap 0.7
        env.engine._thermal_model = {"swing_heat_f": 0.59}
        env.write(66.0)
        env.check()
        assert env.sp_f() == [66.0, 68.0]  # 0.7 > 0.59

    def test_default_swing_when_unlearned(self):
        env = Env(bedroom_f=65.3)
        env.engine._thermal_model = {"swing_heat_f": None}
        env.write(66.0)
        env.check()
        assert env.sp_f() == [66.0]  # 0.7 < 1.5 default

    def test_missing_model_uses_default(self):
        env = Env(bedroom_f=65.3)
        env.engine._thermal_model = {}
        env.write(66.0)
        env.check()
        assert env.sp_f() == [66.0]


# ---------------------------------------------------------------------------
# Engine behaviour: cool mirror
# ---------------------------------------------------------------------------


class TestCoolLoop:
    def _env(self, bedroom_f, **cfg):
        env = Env(mode="cool", bedroom_f=bedroom_f, hall_f=71.0, config=cfg or None)
        env.engine._current_classification = _classification("cool")
        return env

    def test_lowers_setpoint_when_bedroom_above_target_plus_swing(self):
        env = self._env(74.0)  # 72 + 1.5 = 73.5
        env.write(72.0, mode="cool")
        env.check()
        assert env.sp_f() == [72.0, 70.0]

    def test_steps_back_up_when_bedroom_below_target_minus_swing(self):
        env = self._env(74.0)
        env.write(72.0, mode="cool")
        env.check()
        env.set_bedroom(70.0)  # 72 - 1.5 = 70.5 -> below
        env.check()
        assert env.last_f() == 72.0

    def test_cap_and_floor_edge(self):
        env = self._env(80.0)
        env.write(72.0, mode="cool")
        for _ in range(5):
            env.check()
            env.advance(400)
            env.set_bedroom(80.0)
        assert min(env.sp_f()) == 68.0  # 72 - 4
        env2 = self._env(80.0, sleep_heat=71.0)  # floor 71 -> room for 1 only
        env2.write(72.0, mode="cool")
        for _ in range(3):
            env2.check()
            env2.advance(400)
            env2.set_bedroom(80.0)
        assert min(env2.sp_f()) == 71.0


# ---------------------------------------------------------------------------
# Celsius installs: step 1 C, cap 2 C
# ---------------------------------------------------------------------------


class TestCelsius:
    def test_step_one_degree_cap_two_degrees(self):
        env = Env(unit="celsius", bedroom_f=61.0)  # ~16.1 C vs 66F (18.9 C) target
        env.write(66.0)
        base_c = env.sp_calls()[0]
        for _ in range(4):
            env.check()
            env.advance(400)
            env.set_bedroom(61.0)
        sent = env.sp_calls()
        assert sent[1] == pytest.approx(base_c + 1.0, abs=0.06)
        assert max(sent) == pytest.approx(base_c + 2.0, abs=0.06)  # capped at +2 C
        assert len(sent) == 3  # base, +1, +2 -- the 3rd/4th checks add nothing

    def test_hysteresis_is_a_delta_not_an_absolute(self):
        env = Env(unit="celsius", bedroom_f=65.0)  # 1 F under: inside the 1.5 F swing
        env.write(66.0)
        env.check()
        assert len(env.sp_calls()) == 1


# ---------------------------------------------------------------------------
# Freshness (last_reported, 120 min)
# ---------------------------------------------------------------------------


class TestFreshness:
    def test_stale_remote_gets_plain_setpoint(self):
        env = Env(bedroom_f=58.0)
        env.set_bedroom(58.0, reported=NIGHT - timedelta(minutes=130))
        env.write(66.0)
        env.check()
        assert env.sp_f() == [66.0]

    def test_100_minutes_old_is_still_fresh(self):
        env = Env(bedroom_f=58.0)
        env.set_bedroom(58.0, reported=NIGHT - timedelta(minutes=100))
        env.write(66.0)
        env.check()
        assert env.sp_f() == [66.0, 68.0]

    def test_offset_removed_when_remote_goes_stale(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        env.check()
        assert env.last_f() == 68.0
        env.advance(121 * 60)  # sensor never reports again
        env.check()
        assert env.last_f() == 66.0
        n = len(env.sp_f())
        env.check()
        assert len(env.sp_f()) == n

    def test_other_writers_stop_applying_offset_when_stale(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        env.check()  # +2
        env.advance(121 * 60)
        env.write(66.0, reason="30-minute cycle")
        assert env.last_f() == 66.0


# ---------------------------------------------------------------------------
# Startup wait + actuation feedback
# ---------------------------------------------------------------------------


class TestStartupWait:
    def test_no_second_step_before_the_wait(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        env.check()  # raise -> 68
        env.advance(60)
        env.set_bedroom(58.0)
        env.check()
        assert env.sp_f() == [66.0, 68.0]
        env.advance(250)  # total 310 s > default 300 s wait, thermostat never fired
        env.set_bedroom(58.0)
        env.check()
        assert env.sp_f() == [66.0, 68.0, 70.0]

    def test_fired_thermostat_means_hold(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        env.check()  # raise -> 68
        env.advance(10)
        env.set_action("heating")
        env.engine.note_remote_offset_actuation("heat", env.sched.now())
        env.advance(400)  # well past the wait; furnace still running
        env.set_bedroom(58.0)
        env.check()
        assert env.sp_f() == [66.0, 68.0]

    def test_measured_latency_shortens_the_wait(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        env.check()
        env.advance(5)
        env.engine.note_remote_offset_actuation("heat", env.sched.now())
        assert env.engine._remote_startup_latency_s == pytest.approx(5.0, abs=1.0)
        env.set_action("idle")  # a short cycle that already ended
        env.advance(130)  # > 2 min measured wait, < 5 min default
        env.set_bedroom(58.0)
        env.check()
        assert env.sp_f() == [66.0, 68.0, 70.0]

    def test_step_down_is_not_delayed_by_the_wait(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        env.check()  # raise at t0
        env.advance(30)
        env.set_bedroom(68.5)  # register plume: well above 66 + 1.5
        env.check()
        assert env.last_f() == 66.0


# ---------------------------------------------------------------------------
# ONE setpoint path: every CA writer gets the offset; provenance + gates
# ---------------------------------------------------------------------------


class TestSingleSetpointPath:
    def _raised(self):
        env = Env(bedroom_f=64.0)
        env.write(66.0)
        env.check()
        assert env.last_f() == 68.0
        return env

    def test_comfort_band_apply_carries_offset(self):
        env = self._raised()
        band = ComfortBand(floor=66.0, ceiling=72.0, active="floor", reason="sleep band")
        env.run(env.engine._apply_comfort_band(band, reason="30-minute cycle"))
        assert env.last_f() == 68.0

    def test_mode_selector_restore_carries_offset(self):
        env = self._raised()
        env.run(env.engine._set_temperature_for_mode(_classification("heat"), reason="window closed restore"))
        assert env.last_f() == 68.0

    def test_tou_precondition_carries_offset(self):
        env = self._raised()
        env.run(env.engine.apply_tou_precondition(_classification("heat"), 67.0, "sched-1"))
        assert env.last_f() == 69.0  # target 67 + 2

    def test_direct_setpoint_write_carries_offset(self):
        env = self._raised()
        env.write(66.0, reason="wake/bedtime/pre-cool style direct write")
        assert env.last_f() == 68.0

    def test_verify_state_tracks_commanded_value_and_target(self):
        env = self._raised()
        assert env.engine._pending_setpoint_single == 68.0
        assert env.engine._pending_setpoint_target == 66.0

    def test_offset_absent_when_indoor_temp_is_primary(self):
        env = Env(start=DAY, bedroom_f=58.0)  # day: indoor_temp is the primary sensor
        env.write(70.0)
        env.check()
        assert env.sp_f() == [70.0]
        assert env.engine._remote_offset_f == 0.0


class TestGatesKeepOffsetOff:
    """Gating proper is increment (b); (a) must still never write an offset in these states."""

    def _env(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        return env

    def test_manual_override(self):
        env = self._env()
        env.engine._manual_override_active = True
        env.check()
        env.write(66.0)
        assert env.sp_f() == [66.0, 66.0]

    def test_override_confirmation_pending(self):
        env = self._env()
        env.engine._override_confirm_pending = True
        env.check()
        assert env.sp_f() == [66.0]

    def test_door_window_pause(self):
        env = self._env()
        env.engine._paused_by_door = True
        env.check()
        assert env.sp_f() == [66.0]

    def test_away_and_vacation(self):
        for occ in ("away", "vacation"):
            env = self._env()
            env.engine._occupancy_mode = occ
            env.check()
            assert env.sp_f() == [66.0], occ

    def test_whole_house_fan_ownership(self):
        env = self._env()
        env.engine._whf_owns_hvac = lambda: True
        env.check()
        assert env.sp_f() == [66.0]

    def test_offset_resets_when_a_gate_closes_then_reopens(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        env.check()
        assert env.last_f() == 68.0
        env.engine._manual_override_active = True
        env.check()
        env.engine._manual_override_active = False
        env.write(66.0)
        assert env.last_f() == 66.0  # fresh start: no stale offset carried across the gate

    def test_thermostat_in_another_mode_is_left_alone(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        env.hass.states.set(ENTITY, FakeState(state="off", attributes={"hvac_modes": ["off", "heat", "cool"]}))
        env.check()
        assert env.sp_f() == [66.0]

    def test_dry_run_writes_nothing(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        env.engine.dry_run = True
        env.check()
        assert env.sp_f() == [66.0]


class TestStrandedOffset:
    """A gate that closes and reopens with NO setpoint write in between must not leave the offset on the thermostat.

    Occupant outcome: without this the hallway thermostat keeps target + 4F after the window/override/fan/away episode
    ends, the furnace keeps heating and the bedroom overshoots, until some unrelated writer happens to re-send the
    plain target (up to the next 30-minute cycle).
    """

    _GATES = {
        "override": ("_manual_override_active", True, False),
        "pending": ("_override_confirm_pending", True, False),
        "door": ("_paused_by_door", True, False),
        "natvent": ("_natural_vent_active", True, False),
        "away": ("_occupancy_mode", "away", "home"),
    }

    def _raised_to_cap(self) -> Env:
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        env.check()  # 68
        env.advance(400)
        env.set_bedroom(58.0)
        env.check()  # 70
        assert env.hass.states.get(ENTITY).attributes["temperature"] == 70.0
        return env

    @pytest.mark.parametrize("gate", sorted(_GATES))
    def test_plain_target_restored_once_the_gate_reopens(self, gate):
        env = self._raised_to_cap()
        attr, closed, opened = self._GATES[gate]
        setattr(env.engine, attr, closed)
        env.advance(60)
        env.check()  # gated: offset forgotten, nothing written
        assert env.last_f() == 70.0
        setattr(env.engine, attr, opened)
        env.advance(60)
        env.set_bedroom(69.0)  # the bedroom is now warm: nothing should be raising the setpoint
        env.check()
        assert env.hass.states.get(ENTITY).attributes["temperature"] == 66.0, env.sp_f()
        n = len(env.sp_f())
        env.advance(60)
        env.set_bedroom(69.0)
        env.check()
        assert len(env.sp_f()) == n  # one restore write, not one per minute

    def test_whole_house_fan_gate(self):
        env = self._raised_to_cap()
        env.engine._whf_owns_hvac = lambda: True
        env.advance(60)
        env.check()
        env.engine._whf_owns_hvac = lambda: False
        env.advance(60)
        env.set_bedroom(69.0)
        env.check()
        assert env.hass.states.get(ENTITY).attributes["temperature"] == 66.0, env.sp_f()

    def test_no_write_when_the_thermostat_already_holds_the_plain_target(self):
        env = self._raised_to_cap()
        env.engine._paused_by_door = True
        env.check()
        cs = env.hass.states.get(ENTITY)
        env.hass.states.set(ENTITY, FakeState(state="heat", attributes={**cs.attributes, "temperature": 66.0}))
        env.engine._paused_by_door = False
        n = len(env.sp_f())
        env.set_bedroom(66.0)  # in band: desired offset 0
        env.check()
        assert len(env.sp_f()) == n
        assert env.engine._remote_offset_applied_f == 0.0  # known again: the loop diffs on it from here


class TestReassertersNeverDoubleTheOffset:
    """Re-asserting writers pass the comfort TARGET back in; the single path re-applies the offset exactly once.

    Occupant outcome: a doubled offset sends the hallway to target + 2x offset (bounded only by the band ceiling),
    overheating the house on a retry or after the fan starts.
    """

    def _raised_then_reverted(self, config=None) -> Env:
        env = Env(bedroom_f=64.0, config=config)
        env.write(66.0)
        env.check()  # 68 = 66 + 2
        env.set_bedroom(65.0)  # in band from here: the loop itself holds
        return env

    def _thermostat_reports(self, env, f):
        cs = env.hass.states.get(ENTITY)
        env.hass.states.set(ENTITY, FakeState(state="heat", attributes={**cs.attributes, "temperature": f}))

    def test_setpoint_retry(self):
        env = self._raised_then_reverted()
        self._thermostat_reports(env, 66.0)  # the thermostat did not take 68
        env.advance(185)  # the acceptance check waits out the 180 s echo window (#1194 e F1), then the retry is armed
        env.advance(905)  # retry fires
        assert env.sp_f() == [66.0, 68.0, 68.0]

    def test_post_fan_on_reassert(self):
        env = self._raised_then_reverted(config={"fan_mode": "hvac_fan"})
        env.run(env.engine._activate_fan(reason="test"))
        self._thermostat_reports(env, 66.0)  # e.g. an Ecobee reverting to its program after the fan command
        env.advance(31)
        assert env.sp_f() == [66.0, 68.0, 68.0]

    def test_post_fan_off_reassert(self):
        env = self._raised_then_reverted(config={"fan_mode": "hvac_fan"})
        env.run(env.engine._activate_fan(reason="test"))
        env.advance(1800)  # past the fan toggle rate limit
        env.set_bedroom(65.0)
        n = len(env.sp_f())
        env.run(env.engine._deactivate_fan(reason="test", restore_hvac=False))
        self._thermostat_reports(env, 66.0)
        env.advance(31)
        assert env.sp_f()[n:] == [68.0], env.sp_f()


class TestFailedWriteBackOff:
    """The loop's own write not executed -> roll back, back off (15 min AND a new remote report), single step after.

    Occupant outcome: while the thermostat (or its cloud) refuses writes, CA does not hammer it every minute all night,
    and when writes work again the setpoint moves one step from what the thermostat really holds, not straight to +4.
    """

    def _failing(self, env):
        orig = env.hass.services.async_call
        attempts = {"n": 0}

        async def failing(domain, service, data=None, *a, **k):
            if service == "set_temperature":
                attempts["n"] += 1
                raise RuntimeError("thermostat cloud unreachable")
            return await orig(domain, service, data, *a, **k)

        env.hass.services.async_call = failing
        return orig, attempts

    def _minutes(self, env, n, bedroom=58.0, report=True):
        for _ in range(n):
            env.advance(60)
            if report:
                env.set_bedroom(bedroom)
            env.check()

    def test_bounded_attempts_and_no_silent_climb(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        _orig, attempts = self._failing(env)
        self._minutes(env, 60)
        assert 2 <= attempts["n"] <= 5, attempts  # one per 15-minute back-off, not one per minute
        assert env.engine._remote_offset_f == 0.0  # rolled back to what the thermostat holds
        assert env.engine._remote_raise_at is None  # no startup-wait clock for a step never sent

    def test_first_write_after_recovery_is_one_step(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        orig, _attempts = self._failing(env)
        self._minutes(env, 30)
        env.hass.services.async_call = orig
        n = len(env.sp_f())
        self._minutes(env, 16)
        assert env.sp_f()[n] == 68.0, env.sp_f()  # 66 + one step, never straight to 70

    def test_waits_for_a_new_remote_report_too(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        orig, attempts = self._failing(env)
        env.advance(60)
        env.set_bedroom(58.0)
        env.check()
        assert attempts["n"] == 1
        env.hass.services.async_call = orig
        n = len(env.sp_f())
        self._minutes(env, 20, report=False)  # 15 min passed, but the bedroom has not reported since the failure
        assert len(env.sp_f()) == n
        env.set_bedroom(58.0)  # the next report
        env.check()
        assert env.sp_f()[n:] == [68.0]

    def test_another_writers_success_ends_the_back_off(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        orig, _attempts = self._failing(env)
        self._minutes(env, 1)
        env.hass.services.async_call = orig
        env.write(66.0, reason="30-minute cycle")  # succeeds
        assert env.engine._remote_write_failed_at is None
        self._minutes(env, 1)
        assert env.last_f() == 68.0


class TestResolverLogsOnChange:
    """The shared resolver logs the sleep-sensor swap at INFO once per source change, DEBUG otherwise."""

    def test_info_once_per_change(self, caplog):
        import logging

        from custom_components.climate_advisor import indoor_temp

        env = Env(bedroom_f=65.0)
        indoor_temp._logged_indoor_source.clear()

        def resolve(in_window):
            return indoor_temp.resolve_indoor_temp_with_provenance(
                hass=env.hass,
                source="climate_fallback",
                unit="fahrenheit",
                indoor_temp_entity=None,
                climate_entity=ENTITY,
                in_sleep_window=in_window,
                sleep_indoor_temp_entity=BEDROOM,
            )

        with caplog.at_level(logging.DEBUG, logger="custom_components.climate_advisor.indoor_temp"):
            for _ in range(60):
                assert resolve(True).remote_context == "sleep"
            for _ in range(5):
                resolve(False)
            resolve(True)
        infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        debugs = [r for r in caplog.records if r.levelno == logging.DEBUG and "Using sleep" in r.getMessage()]
        assert sum("Using sleep indoor sensor" in m for m in infos) == 2, infos  # entering, and re-entering
        assert sum("source changed to the primary" in m for m in infos) == 1, infos
        assert len(debugs) == 59

    def test_engine_loop_minute_checks_stay_quiet(self, caplog):
        import logging

        from custom_components.climate_advisor import indoor_temp

        indoor_temp._logged_indoor_source.clear()
        env = Env(bedroom_f=65.0)
        env.write(66.0)
        caplog.clear()
        with caplog.at_level(logging.INFO):
            for _ in range(60):
                env.advance(60)
                env.set_bedroom(65.0)
                env.check()
        assert not [r for r in caplog.records if "Using sleep indoor sensor" in r.getMessage()]


class TestCheckBookkeeping:
    def test_check_is_a_noop_before_any_setpoint_was_written(self):
        env = Env(bedroom_f=58.0)
        env.check()
        assert env.sp_f() == []

    def test_reentrant_check_is_ignored(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        env.engine._remote_offset_check_running = True
        env.check()
        assert env.sp_f() == [66.0]

    def test_pre_cool_hold_survives_an_offset_step(self):
        env = Env(bedroom_f=58.0)
        env.write(66.0)
        sentinel = ComfortBand(floor=60.0, ceiling=70.0, active="ceiling", reason="pre-cool")
        env.engine._pre_cool_band_in_force = sentinel
        env.check()
        assert env.last_f() == 68.0
        assert env.engine._pre_cool_band_in_force is sentinel


# ---------------------------------------------------------------------------
# Coordinator wiring
# ---------------------------------------------------------------------------


class TestCoordinatorWiring:
    def test_sleep_listener_runs_the_offset_check(self):
        import asyncio
        import importlib
        import types
        from unittest.mock import AsyncMock, MagicMock, patch

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        cls = mod.ClimateAdvisorCoordinator
        coord = object.__new__(cls)
        coord.config = {"sleep_indoor_temp_entity": BEDROOM, "sleep_time": "22:30", "wake_time": "06:30"}
        ae = MagicMock()
        ae._natural_vent_active = False
        ae._fan_active = False
        ae.comfort_family_temperature_check = AsyncMock()
        ae.remote_offset_check = AsyncMock()
        coord.automation_engine = ae
        coord._get_indoor_temp = MagicMock(return_value=64.0)
        coord._refresh_indoor_sensor_health = MagicMock()
        coord._startup_coalesce_active = False  # steady state (#1194 b: the loop is silent while coalescing)
        coord._last_outdoor_temp = 50.0
        coord._last_predicted_indoor = None
        coord._async_run_temp_reactive_checks = types.MethodType(cls._async_run_temp_reactive_checks, coord)
        coord._on_sleep_indoor_temp_changed = types.MethodType(cls._on_sleep_indoor_temp_changed, coord)
        with patch.object(mod.dt_util, "now", return_value=datetime(2026, 11, 2, 2, 0)):
            asyncio.run(coord._on_sleep_indoor_temp_changed(MagicMock()))
        ae.remote_offset_check.assert_awaited_once()

    def test_equipment_activity_feeds_the_actuation_feedback(self):
        import importlib
        from unittest.mock import MagicMock

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        cls = mod.ClimateAdvisorCoordinator
        coord = object.__new__(cls)
        coord._equipment_last_active = None
        coord.automation_engine = MagicMock()
        coord._note_equipment_activity("heating", "idle")
        coord.automation_engine.note_remote_offset_actuation.assert_called_once()
        assert coord.automation_engine.note_remote_offset_actuation.call_args[0][0] == "heat"

    def _coord(self, bedroom_f=58.0):
        from tools.sim_harness.build_coordinator import build_headless_coordinator

        cfg = dict(BASE_CONFIG)
        co, fh, sch, _ev = build_headless_coordinator(
            config=cfg,
            climate_state="heat",
            climate_attributes={"current_temperature": 67.0, "temperature": 66.0, "hvac_action": "idle"},
            start_time=NIGHT,
            skip_startup_coalesce=True,
        )
        co._resolved_sensors = []
        co.automation_engine._current_classification = _classification("heat")
        fh.states.set(
            BEDROOM,
            FakeState(
                state=str(bedroom_f),
                attributes={"unit_of_measurement": "°F"},
                last_reported=NIGHT,
                last_updated=NIGHT,
            ),
        )
        return co, fh, sch

    def test_one_minute_floor_timer_drives_the_loop(self):
        co, fh, sch = self._coord()
        ae = co.automation_engine
        with sch.installed():
            run_coro(ae._set_temperature(66.0, reason="bedtime", mode="heat"))
            fh.states.set(
                BEDROOM,
                FakeState(
                    state="58.0",
                    attributes={"unit_of_measurement": "°F"},
                    last_reported=sch.now() + timedelta(seconds=30),
                ),
            )
            sch.advance_by(61)
        sets = [a["data"]["temperature"] for a in fh.action_log if a["service"] == "set_temperature"]
        assert sets[:2] == [66.0, 68.0], sets

    def test_bedroom_update_event_drives_the_loop_without_waiting_for_the_timer(self):
        co, fh, sch = self._coord(bedroom_f=65.0)
        ae = co.automation_engine
        with sch.installed():
            run_coro(ae._set_temperature(66.0, reason="bedtime", mode="heat"))
            sch.advance_by(5)  # well inside the 1-minute floor
            fh.states.async_set(BEDROOM, "58.0", {"unit_of_measurement": "°F"})
            fh.states.get(BEDROOM).last_reported = sch.now()
            sch.advance_by(0)
        sets = [a["data"]["temperature"] for a in fh.action_log if a["service"] == "set_temperature"]
        assert 68.0 in sets, sets


_unused = SimpleNamespace  # keep import used if a test above is trimmed
