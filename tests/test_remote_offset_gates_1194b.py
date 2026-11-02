"""Issue #1194 increment (b): the remote-sensor outer loop's GATES, proven through the real coordinator + engine.

Occupant outcome: on a sleep-sensor night Climate Advisor adds extra degrees to the hallway thermostat's setpoint so the
cold bedroom gets heat (the outer loop, increment a). The person in the bedroom must never have those extra degrees
pushed into an open window, over a thermostat change they just made, against a running whole-house fan, or into an
empty house -- and when whatever owned the thermostat lets go they get the right temperature back, not a stranded
+4 F and not a doubled offset.

Every gate is driven the way it happens in a home, not by poking ``_remote_offset_gate()``:
  * the occupant dials the thermostat      -> ``coordinator._async_thermostat_changed`` (override / pending),
  * a window opens                         -> ``coordinator._async_door_window_changed`` + its debounce timer,
  * the whole-house fan is pressed         -> ``coordinator._async_fan_entity_changed``,
  * nat-vent starts by itself              -> the engine's door/window handler,
  * everyone leaves                        -> ``engine.handle_occupancy_away``.
Each runs in BOTH fan configurations (``whole_house_fan``, ``hvac_fan``), heat and (where the gate is
mode-independent) cool mirrored. Cool mode is simulation-tested only.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.learning import DailyRecord
from custom_components.climate_advisor.remote_offset import RemoteOffsetGate, decide_remote_offset_gate
from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.fake_hass import FakeEvent, FakeState
from tools.sim_harness.ha_stubs import install_ha_stubs

install_ha_stubs()

FAN_MODES = ["whole_house_fan", "hvac_fan"]
_FAN_CONFIG = {
    "whole_house_fan": {
        "fan_mode": "whole_house_fan",
        "fan_entity": "fan.whf",
        "fan_state_entity": "fan.whf",
        "fan_state_feedback": True,
    },
    "hvac_fan": {"fan_mode": "hvac_fan"},
}
BEDROOM = "sensor.bedroom_temp"
WINDOW = "binary_sensor.bedroom_window"
FAN = "fan.whf"
NIGHT = datetime(2026, 11, 2, 23, 0, 0, tzinfo=UTC)  # inside the 20:30 -> 06:30 sleep window

# direction: (thermostat mode, comfort target F, "bedroom is far from target" F, "bedroom is on the far side" F,
#             sign of the offset on the thermostat)
DIRECTIONS = {
    "heat": {"mode": "heat", "target": 66.0, "off_target": 58.0, "past_target": 74.0, "sign": +1, "setback": 60.0},
    "cool": {"mode": "cool", "target": 72.0, "off_target": 74.0, "past_target": 70.0, "sign": -1, "setback": 82.0},
}
_BASE_CONFIG = {
    "sleep_time": "20:30",
    "wake_time": "06:30",
    "sleep_heat": 66.0,
    "sleep_cool": 72.0,
    "comfort_heat": 66.0,
    "comfort_cool": 75.0,
    "sleep_indoor_temp_entity": BEDROOM,
    "door_window_sensors": [WINDOW],
    "thermostat_hvac_modes": ["off", "heat", "cool"],
    "sensor_debounce_seconds": 30,
    "manual_grace_seconds": 900,
    "override_confirm_seconds": 120,
    "fan_min_runtime_per_hour": 0,
}


def _classification(mode: str) -> DayClassification:
    return DayClassification(
        day_type="cold" if mode == "heat" else "hot",
        trend_direction="stable",
        trend_magnitude=0,
        today_high=40 if mode == "heat" else 95,
        today_low=25 if mode == "heat" else 70,
        tomorrow_high=40,
        tomorrow_low=25,
        hvac_mode=mode,
    )


class Night:
    """A real coordinator + engine on a sleep-sensor night: hallway thermostat at ``target``, bedroom off target."""

    def __init__(
        self,
        fan_mode: str,
        direction: str = "heat",
        *,
        bedroom_f: float | None = None,
        config: dict | None = None,
        outdoor_f: float = 45.0,
        skip_coalesce: bool = True,
    ) -> None:
        self.d = DIRECTIONS[direction]
        self.fan_mode = fan_mode
        self.target = self.d["target"]
        cfg = {**_BASE_CONFIG, **_FAN_CONFIG[fan_mode], **(config or {})}
        attrs = {
            "current_temperature": self.target + self.d["sign"],  # the hallway reads near its setpoint
            "temperature": self.target,
            "hvac_action": "idle",
            "fan_mode": "auto",
            "fan_modes": ["auto", "on"],
            "hvac_modes": ["off", "heat", "cool"],
        }
        self.co, self.fh, self.sch, self.ev = build_headless_coordinator(
            config=cfg,
            climate_state=self.d["mode"],
            climate_attributes=attrs,
            start_time=NIGHT,
            skip_startup_coalesce=skip_coalesce,
        )
        self.ae = self.co.automation_engine
        self.ce = self.co.config["climate_entity"]
        c = _classification(self.d["mode"])
        self.co._current_classification = c
        self.ae._current_classification = c
        self.co._today_record = DailyRecord(date="2024-01-15", day_type=c.day_type, trend_direction="stable")
        self.co._last_outdoor_temp = outdoor_f
        self.fh.states.set(
            "weather.test_forecast",
            FakeState(state="clear", attributes={"temperature": outdoor_f, "temperature_unit": "\u00b0F"}),
        )
        self.fh.states.set(WINDOW, FakeState(state="off"))
        if fan_mode == "whole_house_fan":
            self.fh.states.set(FAN, FakeState(state="off"))
        self.state = self.fh.states.get(self.ce)
        # Real thermostats answer later than the call; here the test delivers the occupant's changes by hand.
        self.set_bedroom(bedroom_f if bedroom_f is not None else self.d["off_target"])

    # -- world -------------------------------------------------------------------------------------------------
    def set_bedroom(self, f: float) -> None:
        """The bedroom sensor reports ``f`` now (its state event reaches the real sleep-sensor listener)."""
        with self.sch.installed():
            self.fh.states.async_set(BEDROOM, str(f), {"unit_of_measurement": "°F"})
            self.fh.states.get(BEDROOM).last_reported = self.sch.now()
            self.sch.advance_by(0)

    def wait(self, seconds: float) -> None:
        with self.sch.installed():
            self.sch.advance_by(int(seconds))

    def wait_until(self, predicate, *, limit_s: int = 900, step_s: int = 10) -> None:
        """Advance the virtual clock until ``predicate()`` (e.g. the toggle floor lets a deferred fan stop through)."""
        waited = 0
        while not predicate() and waited < limit_s:
            self.wait(step_s)
            waited += step_s
        assert predicate(), f"still not true after {limit_s}s"

    def run(self, coro):
        with self.sch.installed():
            return run_coro(coro)

    def ca_writes(self, target_f: float | None = None, *, reason: str = "bedtime"):
        return self.run(
            self.ae._set_temperature(self.target if target_f is None else target_f, reason=reason, mode=self.d["mode"])
        )

    def window(self, is_open: bool) -> None:
        with self.sch.installed():
            self.fh.states.async_set(WINDOW, "on" if is_open else "off")
            self.sch.advance_by(0)

    def thermostat_reports(self, state: str | None = None, context=None, **attrs) -> None:
        """The thermostat reports a state change that CA did not command (an occupant at the dial / the app)."""
        old = self.fh.states.get(self.ce)
        new_attrs = dict(old.attributes)
        new_attrs.update(attrs)
        new = FakeState(state=state or old.state, attributes=new_attrs)
        self.fh.states.set(self.ce, new)
        event = FakeEvent(
            event_type="state_changed",
            data={"entity_id": self.ce, "old_state": old, "new_state": new},
            context=context,
        )
        with self.sch.installed():
            run_coro(self.co._async_thermostat_changed(event))

    def occupant_dials(self, setpoint_f: float) -> None:
        self.thermostat_reports(temperature=setpoint_f)

    def fan_reports(self, new: str) -> None:
        """The whole-house fan's own state changed with no CA command (the wall switch / RF remote)."""
        old = self.fh.states.get(FAN)
        newstate = FakeState(state=new, attributes=dict(getattr(old, "attributes", {}) or {}))
        self.fh.states.set(FAN, newstate)
        event = FakeEvent(event_type="state_changed", data={"entity_id": FAN, "old_state": old, "new_state": newstate})
        with self.sch.installed():
            run_coro(self.co._async_fan_entity_changed(event))

    # -- reading -----------------------------------------------------------------------------------------------
    @property
    def sp(self) -> float:
        return float(self.fh.states.get(self.ce).attributes["temperature"])

    @property
    def mode(self) -> str:
        return self.fh.states.get(self.ce).state

    def sp_writes(self) -> list[float]:
        return [
            a["data"]["temperature"]
            for a in self.fh.action_log
            if a["domain"] == "climate" and a["service"] == "set_temperature" and "temperature" in a["data"]
        ]

    def writes_since(self, mark: int) -> list[float]:
        return self.sp_writes()[mark:]

    # -- states a gate is entered from -------------------------------------------------------------------------
    def offset_at_cap(self) -> None:
        """CA has written the comfort target and the loop has stepped the thermostat to the +4 F / -4 F cap."""
        self.ca_writes()
        self.wait(61)  # 1-minute floor: first raise
        self.wait(301)  # past the startup wait: second raise
        self.wait(150)  # clear of CA's 30 s / 120 s command-recency guards, so an occupant change reads as one
        want = self.target + self.d["sign"] * 4.0
        assert self.sp == want, f"precondition: thermostat at the cap ({want}), saw {self.sp}: {self.sp_writes()}"
        assert self.ae._remote_offset_applied_f == pytest.approx(4.0)

    def plain(self) -> float:
        return self.target

    def one_step(self) -> float:
        return self.target + self.d["sign"] * 2.0


def _night(fan_mode, direction="heat", **kw) -> Night:
    return Night(fan_mode, direction, **kw)


# ---------------------------------------------------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------------------------------------------------
HEAT_AND_COOL = ["heat", "cool"]


def _assert_exit_is_clean(n: Night, mark: int, *, bedroom_in_band: bool) -> None:
    """After the gate lets go: the thermostat holds the plain target (bedroom fine) or ONE fresh step (bedroom off).

    Never the stranded cap (+4/-4), never target + 2 x offset (a doubled offset), and the writes are a handful, not one
    per minute.
    """
    writes = n.writes_since(mark)
    cap = n.target + n.d["sign"] * 4.0
    assert cap not in writes, f"a stranded offset was written at the cap: {writes}"
    assert all(abs(w - n.target) <= 2.0 + 1e-6 for w in writes), f"doubled or stranded offset: {writes}"
    if bedroom_in_band:
        assert n.sp == n.plain(), f"the plain target must be back (bedroom in band), thermostat holds {n.sp}: {writes}"
    else:
        assert n.sp == n.one_step(), f"one fresh loop step, not the old cap: thermostat holds {n.sp}: {writes}"
    assert len(writes) <= 4, f"write storm after the gate reopened: {writes}"


def _bedroom_in_band(n: Night) -> None:
    n.set_bedroom(n.target)


# ---------------------------------------------------------------------------------------------------------------------
# Gate 1 + 2: the occupant's own thermostat change (confirmed override) and an unconfirmed (pending) change
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("direction", HEAT_AND_COOL)
@pytest.mark.parametrize("fan_mode", FAN_MODES)
class TestOccupantChange:
    """The person in the bedroom turns the thermostat to THEIR number while Climate Advisor holds target +/- 4."""

    def _dial(self, n: Night) -> float:
        value = n.target + n.d["sign"] * 3.0
        n.occupant_dials(value)
        return value

    def test_enter_during_confirmed_override_no_step_and_the_occupants_value_stays(self, fan_mode, direction):
        n = _night(fan_mode, direction)
        n.offset_at_cap()
        mark = len(n.sp_writes())
        value = self._dial(n)
        assert n.ae._override_confirm_pending, "precondition: the real listener saw the occupant (pending)"
        assert n.co._today_record.manual_overrides == 1, "the occupant's change was not counted"
        n.wait(130)  # past the 120 s confirmation window
        assert n.ae._manual_override_active, "precondition: the change was confirmed"
        # The bedroom swings to BOTH sides of the hysteresis band: neither a raise nor a step-down may be written.
        for f in (n.d["off_target"], n.d["past_target"], n.d["off_target"]):
            n.set_bedroom(f)
            n.wait(130)
        assert n.writes_since(mark) == [], f"CA wrote over the occupant's value: {n.writes_since(mark)}"
        assert n.sp == value, "the occupant's setpoint did not survive"

    def test_during_pending_change_no_step(self, fan_mode, direction):
        n = _night(fan_mode, direction, config={"override_confirm_seconds": 600})
        n.offset_at_cap()
        mark = len(n.sp_writes())
        value = self._dial(n)
        for f in (n.d["off_target"], n.d["past_target"]):
            n.set_bedroom(f)
            n.wait(200)
        assert n.ae._override_confirm_pending and not n.ae._manual_override_active, "precondition: still unconfirmed"
        assert n.writes_since(mark) == [], f"CA stepped while the change was unconfirmed: {n.writes_since(mark)}"
        assert n.sp == value

    @pytest.mark.parametrize("bedroom_in_band", [True, False])
    def test_exit_plain_target_or_one_fresh_step(self, fan_mode, direction, bedroom_in_band):
        n = _night(fan_mode, direction)
        n.offset_at_cap()
        self._dial(n)
        n.wait(130)
        assert n.ae._manual_override_active
        if bedroom_in_band:
            _bedroom_in_band(n)
        mark = len(n.sp_writes())
        n.ae.cancel_override(reason="user_cancel_override")  # the dashboard's Cancel Override
        n.wait(130)
        assert not n.ae._manual_override_active
        _assert_exit_is_clean(n, mark, bedroom_in_band=bedroom_in_band)

    def test_exit_by_grace_expiry(self, fan_mode, direction):
        """The override ends the ordinary way: its manual grace runs out (900 s)."""
        n = _night(fan_mode, direction)
        n.offset_at_cap()
        self._dial(n)
        n.wait(130)
        assert n.ae._manual_override_active
        _bedroom_in_band(n)
        mark = len(n.sp_writes())
        n.wait(900)
        assert not n.ae._manual_override_active, "precondition: the grace expired"
        n.wait(130)
        _assert_exit_is_clean(n, mark, bedroom_in_band=True)

    def test_pending_change_that_is_cancelled_exits_clean(self, fan_mode, direction):
        n = _night(fan_mode, direction, config={"override_confirm_seconds": 600})
        n.offset_at_cap()
        self._dial(n)
        _bedroom_in_band(n)
        mark = len(n.sp_writes())
        n.ae.cancel_override(reason="user_cancel_override")
        n.wait(130)
        assert not n.ae._override_confirm_pending
        _assert_exit_is_clean(n, mark, bedroom_in_band=True)


# ---------------------------------------------------------------------------------------------------------------------
# Gate 3: a door/window (rule 3) -- inside the debounce, after the pause, and on close
# ---------------------------------------------------------------------------------------------------------------------
# Outdoor air on a night where opening the window can NOT start natural ventilation (so the door/window handler PAUSES).
PAUSE_OUTDOOR_F = {"heat": 45.0, "cool": 80.0}


@pytest.mark.parametrize("direction", HEAT_AND_COOL)
@pytest.mark.parametrize("fan_mode", FAN_MODES)
class TestDoorWindow:
    """The window next to the bed opens: no extra degrees into it, then the right temperature when it closes."""

    def test_inside_the_debounce_the_loop_does_not_raise(self, fan_mode, direction):
        n = _night(fan_mode, direction, config={"sensor_debounce_seconds": 300}, outdoor_f=PAUSE_OUTDOOR_F[direction])
        n.ca_writes()  # plain target, offset 0, bedroom off target: the loop would raise at its next check
        mark = len(n.sp_writes())
        n.window(True)
        assert n.ae._sensor_debounce_pending and not n.ae._paused_by_door, "precondition: inside the debounce"
        assert n.ae._any_monitored_sensor_settled_open() is False, "precondition: not yet a settled-open window"
        n.wait(200)  # three 1-minute checks and a bedroom report
        n.set_bedroom(n.d["off_target"])
        n.wait(60)
        # The window event refreshes the band, which may re-assert the PLAIN target; it must never add degrees.
        writes = n.writes_since(mark)
        assert all(w == n.plain() for w in writes), f"extra degrees were added into an open window: {writes}"
        assert n.sp == n.plain()

    def test_inside_the_debounce_the_held_offset_is_kept_not_stepped_or_withdrawn(self, fan_mode, direction):
        """FREEZE (owner decision D1): the step already on the thermostat stays exactly as it is until the pause."""
        n = _night(fan_mode, direction, config={"sensor_debounce_seconds": 300}, outdoor_f=PAUSE_OUTDOOR_F[direction])
        n.ca_writes()
        n.wait(61)  # first raise: one step applied
        assert n.sp == n.one_step()
        n.wait(150)
        mark = len(n.sp_writes())
        raised_at = n.ae._remote_raise_at
        n.window(True)
        n.wait(100)  # past the 300 s startup wait since the raise: outside the freeze the loop would raise again
        # ... and a report beyond target + swing (1.5 F) would step it down: neither may happen. (Kept inside the
        # comfort band so no band writer switches its own target.)
        n.set_bedroom(n.target - n.d["sign"] * 2.5)
        n.wait(60)
        n.set_bedroom(n.d["off_target"])
        n.wait(30)
        assert n.ae._sensor_debounce_pending and not n.ae._paused_by_door, "precondition: still inside the debounce"
        writes = n.writes_since(mark)
        assert set(writes) <= {n.one_step()}, f"the held offset was stepped or withdrawn inside the debounce: {writes}"
        assert n.sp == n.one_step()
        assert n.ae._remote_offset_f == pytest.approx(2.0) and n.ae._remote_offset_applied_f == pytest.approx(2.0)
        assert n.ae._remote_raise_at == raised_at, "the freeze must not reset the loop's startup-wait clock"

    def test_a_refresh_during_the_freeze_re_applies_the_held_offset(self, fan_mode, direction):
        """Another writer (the #489 refresh -> apply_classification -> band apply) writes the HELD offset, not 0."""
        n = _night(fan_mode, direction, config={"sensor_debounce_seconds": 300}, outdoor_f=PAUSE_OUTDOOR_F[direction])
        n.offset_at_cap()
        cap = n.target + n.d["sign"] * 4.0
        n.window(True)
        n.wait(5)
        mark = len(n.sp_writes())
        n.run(n.ae.apply_classification(n.ae._current_classification))
        n.run(n.co.async_request_refresh())
        n.wait(5)
        writes = n.writes_since(mark)
        assert writes, "precondition: the band writer wrote during the freeze"
        assert set(writes) == {cap}, f"a writer dropped or stepped the held offset during the freeze: {writes}"
        assert n.sp == cap

    @pytest.mark.parametrize("bedroom_in_band", [True, False])
    def test_a_blip_that_closes_inside_the_debounce_changes_nothing(self, fan_mode, direction, bedroom_in_band):
        """Nothing happened: the offset continues, no setpoint change, no startup-wait reset."""
        n = _night(fan_mode, direction, config={"sensor_debounce_seconds": 300}, outdoor_f=PAUSE_OUTDOOR_F[direction])
        n.offset_at_cap()
        cap = n.target + n.d["sign"] * 4.0
        raised_at = n.ae._remote_raise_at
        if bedroom_in_band:
            _bedroom_in_band(n)  # inside the hysteresis band: the loop holds the cap
        mark = len(n.sp_writes())
        n.window(True)
        n.wait(60)
        n.window(False)
        n.wait(180)
        assert not n.ae._paused_by_door and n.mode == n.d["mode"]
        writes = n.writes_since(mark)
        assert set(writes) <= {cap}, f"a blip changed the setpoint: {writes}"
        assert n.sp == cap and n.ae._remote_offset_f == pytest.approx(4.0)
        assert n.ae._remote_raise_at == raised_at

    @pytest.mark.parametrize("period_s, open_s", [(120, 10), (180, 20)])
    def test_a_busy_door_never_flaps_the_setpoint(self, fan_mode, direction, period_s, open_s):
        """A door used every few minutes for an hour: 0 setpoint changes (before the freeze: 40-60 per hour)."""
        n = _night(fan_mode, direction, config={"sensor_debounce_seconds": 600}, outdoor_f=PAUSE_OUTDOOR_F[direction])
        n.offset_at_cap()
        start = n.sp
        mark = len(n.sp_writes())
        for _ in range(3600 // period_s):
            n.window(True)
            n.wait(open_s)
            n.window(False)
            n.wait(period_s - open_s)
        writes = n.writes_since(mark)
        changes = sum(1 for prev, cur in zip([start, *writes], writes, strict=False) if cur != prev)
        assert changes == 0, f"{changes} setpoint changes in an hour of door use: {writes}"
        assert n.sp == start

    def test_an_open_that_outlasts_the_debounce_pauses_then_exits_clean(self, fan_mode, direction):
        """Frozen inside the debounce, then the pause (rule 3: HVAC off) owns it, then the plain target on close."""
        n = _night(fan_mode, direction, config={"sensor_debounce_seconds": 300}, outdoor_f=PAUSE_OUTDOOR_F[direction])
        n.offset_at_cap()
        cap = n.target + n.d["sign"] * 4.0
        n.window(True)
        n.wait(200)
        assert n.ae._sensor_debounce_pending and n.sp == cap, "precondition: frozen at the cap inside the debounce"
        n.wait(120)
        assert n.ae._paused_by_door and n.mode == "off", "the open window past its debounce must pause HVAC (rule 3)"
        mark = len(n.sp_writes())
        n.wait(130)
        assert n.writes_since(mark) == [], f"CA wrote a setpoint into the paused, open window: {n.writes_since(mark)}"
        _bedroom_in_band(n)
        mark = len(n.sp_writes())
        n.window(False)
        n.wait(130)
        assert not n.ae._paused_by_door and n.mode == n.d["mode"], "precondition: HVAC is back"
        _assert_exit_is_clean(n, mark, bedroom_in_band=True)

    def test_after_the_pause_nothing_is_written_and_the_close_restores_the_plain_target(self, fan_mode, direction):
        n = _night(fan_mode, direction, outdoor_f=PAUSE_OUTDOOR_F[direction])
        n.offset_at_cap()
        n.window(True)
        n.wait(40)  # past the 30 s debounce
        assert n.ae._paused_by_door and n.mode == "off", "precondition: the pause switched HVAC off"
        mark = len(n.sp_writes())
        for f in (n.d["off_target"], n.d["past_target"]):
            n.set_bedroom(f)
            n.wait(130)
        assert n.mode == "off"
        assert n.writes_since(mark) == [], f"CA wrote a setpoint while the window was open: {n.writes_since(mark)}"
        _bedroom_in_band(n)
        mark = len(n.sp_writes())
        n.window(False)
        n.wait(130)
        assert not n.ae._paused_by_door and n.mode == n.d["mode"], "precondition: HVAC is back"
        _assert_exit_is_clean(n, mark, bedroom_in_band=True)

    def test_close_with_the_bedroom_still_off_target_gets_one_fresh_step_not_the_old_cap(self, fan_mode, direction):
        n = _night(fan_mode, direction, outdoor_f=PAUSE_OUTDOOR_F[direction])
        n.offset_at_cap()
        n.window(True)
        n.wait(40)
        n.wait(300)
        mark = len(n.sp_writes())
        n.window(False)
        n.wait(130)
        assert not n.ae._paused_by_door
        _assert_exit_is_clean(n, mark, bedroom_in_band=False)


# ---------------------------------------------------------------------------------------------------------------------
# Gate 4: fan ownership -- natural ventilation (both fans), the occupant's whole-house-fan press, the thermostat blower
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fan_mode", FAN_MODES)
class TestNaturalVentilationOwnsTheThermostat:
    """A hot night: AC offset applied, then the window opens and the engine starts natural ventilation by itself.

    whole_house_fan: the fan runs and owns the HVAC (rule 2). hvac_fan: the thermostat blower is the fan and the
    thermostat stays Off while it ventilates through the open window (#1142 D7).
    """

    def _ventilating(self, fan_mode: str) -> Night:
        n = _night(fan_mode, "cool", outdoor_f=66.0)
        n.offset_at_cap()
        n.window(True)
        n.wait(40)  # past the 30 s debounce
        assert n.ae._natural_vent_active, "precondition: the open window started natural ventilation"
        assert n.mode == "off", "precondition: the thermostat is held Off while the fan ventilates"
        assert n.ae._whf_owns_hvac() is (fan_mode == "whole_house_fan")
        n.wait(70)  # a loop tick sees the gate closed (a gate shorter than one tick changes nothing)
        return n

    def test_during_the_session_the_loop_writes_nothing(self, fan_mode):
        n = self._ventilating(fan_mode)
        mark = len(n.sp_writes())
        for f in (n.d["past_target"], n.d["off_target"], n.d["past_target"]):
            n.set_bedroom(f)
            n.wait(130)
        assert n.ae._natural_vent_active
        assert n.mode == "off"
        assert n.writes_since(mark) == [], f"CA wrote a setpoint during natural ventilation: {n.writes_since(mark)}"

    @pytest.mark.parametrize("bedroom_in_band", [True, False])
    def test_exit_plain_target_or_one_fresh_step(self, fan_mode, bedroom_in_band):
        n = self._ventilating(fan_mode)
        if bedroom_in_band:
            _bedroom_in_band(n)
        mark = len(n.sp_writes())
        n.window(False)
        # The fan's toggle floor may defer the stop (and the AC restore waits for it).
        n.wait_until(lambda: not n.ae._natural_vent_active and n.mode == "cool")
        n.wait(130)
        _assert_exit_is_clean(n, mark, bedroom_in_band=bedroom_in_band)


@pytest.mark.parametrize("direction", HEAT_AND_COOL)
class TestOccupantWholeHouseFanPress:
    """whole_house_fan home: the person (or a phantom RF press, #1198) turns the fan on.

    A whole-house fan with NO door/window sensor is an unsupported configuration (#1200); it is used here only to
    isolate the WHF-ownership term from the open-window term (on a supported configuration an open window always
    accompanies the fan). The supported-configuration outcome test is
    ``test_occupant_presses_the_fan_with_the_window_open_supported_config``.
    """

    def _pressed(self, direction: str) -> Night:
        n = _night("whole_house_fan", direction, config={"door_window_sensors": []})
        n.offset_at_cap()
        n.fan_reports("on")
        n.wait(70)  # a loop tick sees the gate closed (a gate shorter than one tick changes nothing)
        assert n.ae._whf_owns_hvac() and n.mode == "off", "precondition: the fan owns the HVAC (rule 2)"
        return n

    def test_during_the_loop_writes_nothing(self, direction):
        n = self._pressed(direction)
        mark = len(n.sp_writes())
        for f in (n.d["past_target"], n.d["off_target"]):
            n.set_bedroom(f)
            n.wait(130)
        assert n.ae._whf_owns_hvac()
        assert n.writes_since(mark) == [], f"CA wrote a setpoint against the running fan: {n.writes_since(mark)}"

    @pytest.mark.parametrize("bedroom_in_band", [True, False])
    def test_exit_plain_target_or_one_fresh_step(self, direction, bedroom_in_band):
        n = self._pressed(direction)
        if bedroom_in_band:
            _bedroom_in_band(n)
        mark = len(n.sp_writes())
        n.fan_reports("off")
        n.wait(130)
        assert not n.ae._whf_owns_hvac() and n.mode == n.d["mode"], "precondition: the fan stood down, HVAC is back"
        _assert_exit_is_clean(n, mark, bedroom_in_band=bedroom_in_band)


@pytest.mark.parametrize("bedroom_in_band", [True, False])
@pytest.mark.parametrize("direction", HEAT_AND_COOL)
def test_occupant_presses_the_fan_with_the_window_open_supported_config(direction, bedroom_in_band):
    """Occupant outcome on the SUPPORTED configuration (door/window sensor present, #1200).

    The window is open (inside its debounce, so the offset is frozen at the cap) and the occupant switches the
    whole-house fan on: the fan owns the HVAC (rule 2), the thermostat goes Off and Climate Advisor writes no setpoint
    against the running fan. When the fan is switched off and the window closed the thermostat gets the plain target
    or one fresh step -- never the stranded cap, never a doubled offset.
    """
    n = _night(
        "whole_house_fan", direction, config={"sensor_debounce_seconds": 300}, outdoor_f=PAUSE_OUTDOOR_F[direction]
    )
    n.offset_at_cap()
    n.window(True)
    n.wait(5)
    n.fan_reports("on")
    n.wait(70)
    assert n.ae._whf_owns_hvac() and n.mode == "off", "precondition: the fan owns the HVAC (rule 2)"
    mark = len(n.sp_writes())
    for f in (n.d["past_target"], n.d["off_target"]):
        n.set_bedroom(f)
        n.wait(130)
    assert n.ae._whf_owns_hvac() and n.mode == "off"
    assert n.writes_since(mark) == [], f"CA wrote a setpoint against the running fan: {n.writes_since(mark)}"
    if bedroom_in_band:
        _bedroom_in_band(n)
    mark = len(n.sp_writes())
    n.fan_reports("off")
    n.wait(30)
    n.window(False)
    n.wait(200)
    assert not n.ae._whf_owns_hvac() and n.mode == n.d["mode"], "precondition: the fan stood down, HVAC is back"
    _assert_exit_is_clean(n, mark, bedroom_in_band=bedroom_in_band)


@pytest.mark.parametrize("direction", HEAT_AND_COOL)
def test_phantom_press_in_a_sealed_house_never_doubles_the_offset(direction):
    """Supported configuration (door/window sensor, all closed): a phantom whole-house-fan press (#1198).

    CA's sealed-house safety stands the fan down within seconds and restores the HVAC. The gate was closed for less than
    one loop tick, so the loop's state is untouched: the restore writes back what the thermostat held (the cap), never
    above it. A sanity check only (no tick sees the gate, so it passes with the WHF term deleted); the occupant-outcome
    test for the fan gate on a supported configuration is ``test_occupant_presses_the_fan_with_the_window_open_...``.
    """
    n = _night("whole_house_fan", direction)
    n.offset_at_cap()
    mark = len(n.sp_writes())
    n.fan_reports("on")
    n.wait(400)
    assert not n.ae._whf_owns_hvac() and n.mode == n.d["mode"], "precondition: CA stood the fan down, HVAC is back"
    writes = n.writes_since(mark)
    assert all(abs(w - n.target) <= 4.0 + 1e-6 for w in writes), f"doubled offset: {writes}"
    assert abs(n.sp - n.target) <= 4.0 + 1e-6
    assert len(writes) <= 4, f"write storm: {writes}"
    assert n.ae._remote_offset_f <= 4.0 + 1e-6


@pytest.mark.parametrize("direction", HEAT_AND_COOL)
@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_thermostat_blower_press_and_fan_off_grace_do_not_gate_the_loop(fan_mode, direction):
    """hvac_fan: the occupant's blower press is a fan override; whole_house_fan: the blower is their own equipment.

    Neither turns heat/cool into an open window or against a fan (the blower does not fight the compressor), so the
    loop keeps working: it steps the offset down when the room is warm, and after the blower is switched off (a
    fan-off grace with no override behind it) it raises again. Never doubled.
    """
    n = _night(fan_mode, direction)
    n.offset_at_cap()
    n.thermostat_reports(fan_mode="on")
    n.wait(5)
    if fan_mode == "hvac_fan":
        assert n.ae._fan_override_active and n.ae._fan_override_is_hvac_fan and n.ae._grace_active
    assert not n.ae._manual_change_protected() and not n.ae._whf_owns_hvac()
    mark = len(n.sp_writes())
    n.set_bedroom(n.d["past_target"])  # a bedroom report is an immediate loop check: exactly one step down
    n.wait(5)
    assert n.sp == n.one_step(), f"the warm bedroom must step the offset down even with the blower on: {n.sp_writes()}"
    n.thermostat_reports(fan_mode="auto")
    n.wait(5)
    # hvac_fan: a grace (the blower press's / the fan-off hold) is still running, with no thermostat override behind it.
    # whole_house_fan: the thermostat blower is the occupant's own equipment (#1197): no override, no grace at all.
    assert not n.ae._manual_change_protected()
    if fan_mode == "hvac_fan":
        assert n.ae._grace_active
    n.set_bedroom(n.d["off_target"])
    n.wait(400)
    assert n.sp == n.target + n.d["sign"] * 4.0, "a bare fan-off grace must not stop the loop raising again"
    assert max(abs(w - n.target) for w in n.writes_since(mark)) <= 4.0 + 1e-6


def test_the_systems_own_blower_session_on_an_hvac_fan_home_is_not_a_gate():
    """CA's own thermostat-blower session (circulation / min-runtime, not natural ventilation) runs WITH heat."""
    n = _night("hvac_fan", "heat")
    n.offset_at_cap()
    n.run(n.ae._activate_fan(reason="test circulation"))
    n.wait(5)
    assert n.ae._fan_active and not n.ae._natural_vent_active, "precondition: a plain blower session"
    assert n.mode == "heat" and n.ae._remote_offset_applied_f == pytest.approx(4.0)
    n.set_bedroom(n.d["past_target"])
    n.wait(5)
    assert n.sp == n.one_step(), f"the loop must keep stepping during CA's own blower session: {n.sp_writes()}"


# ---------------------------------------------------------------------------------------------------------------------
# Gate 5: away / vacation (the setback owns the thermostat; an empty house is not heated or cooled)
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("occupancy", ["away", "vacation"])
@pytest.mark.parametrize("direction", HEAT_AND_COOL)
@pytest.mark.parametrize("fan_mode", FAN_MODES)
class TestAwayAndVacation:
    def _empty(self, n: Night, occupancy: str) -> None:
        handler = n.ae.handle_occupancy_away if occupancy == "away" else n.ae.handle_occupancy_vacation
        n.run(handler())
        n.wait(5)
        assert n.ae._occupancy_mode == occupancy

    def test_enter_setback_without_offset_and_during_nothing_is_written(self, fan_mode, direction, occupancy):
        n = _night(fan_mode, direction)
        n.offset_at_cap()
        self._empty(n, occupancy)
        setback = n.sp
        assert abs(setback - n.target) >= 6.0, f"the setback must be written without the offset: {setback}"
        mark = len(n.sp_writes())
        for f in (n.d["off_target"], n.d["past_target"], n.d["off_target"]):
            n.set_bedroom(f)
            n.wait(130)
        # The 30-minute cycle may re-assert the setback itself; it must never be anything else (no offset, no comfort).
        assert set(n.writes_since(mark)) <= {setback}, f"CA heated/cooled an empty house: {n.writes_since(mark)}"
        assert n.sp == setback

    @pytest.mark.parametrize("bedroom_in_band", [True, False])
    def test_exit_plain_target_or_one_fresh_step(self, fan_mode, direction, occupancy, bedroom_in_band):
        n = _night(fan_mode, direction)
        n.offset_at_cap()
        self._empty(n, occupancy)
        if bedroom_in_band:
            _bedroom_in_band(n)
        mark = len(n.sp_writes())
        n.run(n.ae.handle_occupancy_home())
        n.wait(130)
        assert n.ae._occupancy_mode == "home"
        _assert_exit_is_clean(n, mark, bedroom_in_band=bedroom_in_band)


# ---------------------------------------------------------------------------------------------------------------------
# Grace with no override behind it is NOT a gate (the one ownership predicate is _manual_change_protected)
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_grace_left_running_after_the_override_cleared_does_not_gate(fan_mode):
    """Bedtime / wake-up / occupancy handlers clear the override flags and leave its grace timer running (by design).

    They then write their own band through the single path, so the loop must not wait for the grace: the occupant's old
    dial value is not CA's to protect any more, and holding the loop for the grace's remaining minutes would leave the
    bedroom cold for nothing.
    """
    n = _night(fan_mode, "heat")
    n.offset_at_cap()
    n.occupant_dials(69.0)
    n.wait(130)
    assert n.ae._manual_override_active and n.ae._grace_active
    n.ae.clear_manual_override("wake-up")
    assert n.ae._grace_active and not n.ae._manual_change_protected(), "precondition: a grace with nothing behind it"
    _bedroom_in_band(n)
    mark = len(n.sp_writes())
    n.wait(130)
    assert n.sp == n.plain(), f"the loop resumed from a clean slate: {n.sp_writes()[mark:]}"


# ---------------------------------------------------------------------------------------------------------------------
# Startup coalescing window (#1235: occupant changes in the first 300 s are unsupported; CA must not add writes there)
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fan_mode", FAN_MODES)
class TestStartupCoalescing:
    def test_after_a_restart_the_loop_has_no_target_and_stays_silent_in_the_window(self, fan_mode):
        n = _night(fan_mode, "heat", skip_coalesce=False)
        assert n.co._startup_coalesce_active
        n.wait(299)
        assert n.ae._remote_target_f is None and n.sp_writes() == [], "the loop is inert until CA's first write"

    def test_a_write_inside_the_window_is_not_followed_by_loop_steps_until_it_ends(self, fan_mode):
        n = _night(fan_mode, "heat", skip_coalesce=False)
        n.ca_writes()  # some other writer (a door close, an occupancy change) wrote a setpoint inside the window
        assert n.co._startup_coalesce_active
        mark = len(n.sp_writes())
        n.wait(200)
        n.set_bedroom(n.d["off_target"])
        n.wait(60)
        assert n.co._startup_coalesce_active
        assert n.writes_since(mark) == [], f"the loop wrote inside the coalescing window: {n.writes_since(mark)}"

    def test_the_loop_resumes_when_the_window_ends(self, fan_mode):
        n = _night(fan_mode, "heat", skip_coalesce=False)
        n.ca_writes()
        n.wait(330)
        n.set_bedroom(n.d["off_target"])
        n.wait(130)
        assert not n.co._startup_coalesce_active
        assert abs(n.sp - n.target) >= 2.0 - 1e-6, f"the loop never resumed after the window: {n.sp_writes()}"


# ---------------------------------------------------------------------------------------------------------------------
# The ONE predicate: a truth table over the composed pieces (supplements the real-path tests above)
# ---------------------------------------------------------------------------------------------------------------------
_CLOSES_THE_GATE = {
    "confirmed override": lambda n: setattr(n.ae, "_manual_override_active", True),
    "pending override": lambda n: setattr(n.ae, "_override_confirm_pending", True),
    "door/window pause": lambda n: setattr(n.ae, "_paused_by_door", True),
    "natural ventilation": lambda n: setattr(n.ae, "_natural_vent_active", True),
    "whole-house fan owns HVAC": lambda n: setattr(n.ae, "_whf_owns_hvac", lambda: True),
    "away": lambda n: setattr(n.ae, "_occupancy_mode", "away"),
    "vacation": lambda n: setattr(n.ae, "_occupancy_mode", "vacation"),
    "an owner while a window is also inside its debounce": lambda n: (
        n.window(True),
        setattr(n.ae, "_occupancy_mode", "away"),
    ),
}
_DOES_NOT_CLOSE_THE_GATE = {
    "automation grace": lambda n: (
        setattr(n.ae, "_grace_active", True),
        setattr(n.ae, "_last_resume_source", "automation"),
    ),
    "manual grace, no override": lambda n: (
        setattr(n.ae, "_grace_active", True),
        setattr(n.ae, "_last_resume_source", "manual"),
    ),
    "override-protecting grace, override gone": lambda n: (
        setattr(n.ae, "_grace_active", True),
        setattr(n.ae, "_grace_protects_override", True),
    ),
    "a fan override (blower / fan press)": lambda n: setattr(n.ae, "_fan_override_active", True),
    "fan active (CA's own fan session)": lambda n: setattr(n.ae, "_fan_active", True),
}


@pytest.mark.parametrize("fan_mode", FAN_MODES)
class TestGatePredicateTable:
    def test_open_gate_when_nothing_owns_the_thermostat(self, fan_mode):
        n = _night(fan_mode, "heat", config={"sensor_debounce_seconds": 300})
        assert n.ae._remote_offset_gate() is RemoteOffsetGate.PROCEED

    @pytest.mark.parametrize("what", sorted(_CLOSES_THE_GATE))
    def test_each_owner_closes_the_gate(self, fan_mode, what):
        n = _night(fan_mode, "heat", config={"sensor_debounce_seconds": 300})
        _CLOSES_THE_GATE[what](n)
        assert n.ae._remote_offset_gate() is RemoteOffsetGate.CLOSED, what

    def test_a_window_inside_its_debounce_freezes(self, fan_mode):
        n = _night(fan_mode, "heat", config={"sensor_debounce_seconds": 300})
        n.window(True)
        assert n.ae._sensor_debounce_pending
        assert n.ae._remote_offset_gate() is RemoteOffsetGate.FREEZE

    def test_a_window_open_past_its_debounce_without_a_pause_closes(self, fan_mode):
        """E.g. the occupant resumed into an open window: no extra degrees into a settled-open window."""
        n = _night(fan_mode, "heat")
        n.window(True)
        n.wait(40)  # past the 30 s debounce
        n.ae._paused_by_door = False  # isolate the sensor term from the pause flag
        with n.sch.installed():  # the debounce reads the (virtual) clock
            assert not n.ae._sensor_debounce_pending
            assert n.ae._remote_offset_gate() is RemoteOffsetGate.CLOSED

    @pytest.mark.parametrize("what", sorted(_DOES_NOT_CLOSE_THE_GATE))
    def test_a_bare_grace_or_fan_flag_does_not(self, fan_mode, what):
        n = _night(fan_mode, "heat", config={"sensor_debounce_seconds": 300})
        _DOES_NOT_CLOSE_THE_GATE[what](n)
        assert n.ae._remote_offset_gate() is RemoteOffsetGate.PROCEED, what

    def test_a_sensor_that_went_dark_after_reading_open_still_holds_the_gate(self, fan_mode):
        n = _night(fan_mode, "heat")
        n.window(True)
        n.wait(40)  # settled open, paused
        n.fh.states.set(WINDOW, FakeState(state="unavailable"))
        n.ae._paused_by_door = False  # isolate the sensor term from the pause flag
        assert n.ae._any_monitored_sensor_holding_open(), "precondition: the dark sensor still holds open (#1181)"
        assert n.ae._remote_offset_gate() is RemoteOffsetGate.CLOSED

    def test_the_gate_is_consulted_by_both_the_write_path_and_the_check(self, fan_mode):
        """No second copy of the gate: both readers go through ``_remote_offset_gate`` and nothing else decides."""
        import inspect

        from custom_components.climate_advisor.automation import AutomationEngine

        for name in ("_remote_offset_for_write", "_remote_offset_check_locked"):
            src = inspect.getsource(getattr(AutomationEngine, name))
            assert "self._remote_offset_gate()" in src, name
            assert "decide_scheduled_band_gate" not in src and "_any_monitored_sensor" not in src, name


@pytest.mark.parametrize(
    ("owner", "sensor_open", "pending", "expected"),
    [
        (False, False, False, RemoteOffsetGate.PROCEED),
        (False, False, True, RemoteOffsetGate.PROCEED),
        (False, True, True, RemoteOffsetGate.FREEZE),
        (False, True, False, RemoteOffsetGate.CLOSED),
        (True, False, False, RemoteOffsetGate.CLOSED),
        (True, True, True, RemoteOffsetGate.CLOSED),
        (True, True, False, RemoteOffsetGate.CLOSED),
    ],
)
def test_the_pure_gate_rule(owner, sensor_open, pending, expected):
    assert decide_remote_offset_gate(owner_holds=owner, sensor_open=sensor_open, debounce_pending=pending) is expected
