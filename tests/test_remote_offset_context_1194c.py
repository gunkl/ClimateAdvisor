"""Issue #1194 increment (c): explicit remote-context ENTRY and EXIT of the bedroom-sensor outer loop.

Occupant outcome: on a sleep-sensor night Climate Advisor adds extra degrees to the hallway thermostat's setpoint so the
cold bedroom gets heat (the outer loop, increment a). Going to bed and getting up must hand the thermostat over
cleanly:

* ENTRY (the evening comfort zone -> the sleep comfort zone): the night starts from no extra degrees, and the loop never
  acts on the EVENING target before the bedtime band has been written (that would put the bedroom offset on a number
  the sleep zone never meant to command).
* EXIT (the sleep comfort zone -> the morning comfort zone, or the sensor going away): the morning setpoint is the plain
  morning target, never the morning target plus the bedroom's extra degrees, and the thermostat is never left at
  last night's target + 4 F. The wake routine and the loop never write two competing values in the same minute.

Everything runs through the real coordinator + engine rig of ``tests/test_remote_offset_gates_1194b.py`` (time-change
triggers for bedtime/wake, the 1-minute loop floor, the bedroom listener), heat and cool, in both fan configurations.
The context is keyed on ``IndoorTempReading.remote_context`` only; ``TestAnyFutureContext`` proves a second context
name needs no loop change. Cool mode is simulation-tested only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import tests.test_remote_offset_gates_1194b as gates
from custom_components.climate_advisor.const import REMOTE_OFFSET_EXIT_GRACE_S, REMOTE_OFFSET_MIN_WAIT_S
from custom_components.climate_advisor.indoor_temp import IndoorTempReading
from tests.test_remote_offset_gates_1194b import BEDROOM, DIRECTIONS, FAN_MODES, WINDOW, Night
from tools.sim_harness._loop import run_coro

HEAT_AND_COOL = ["heat", "cool"]
# The evening / morning zone differs from the sleep zone (and its target is neither the sleep cap nor past the band).
DAY_DEFAULTS = {
    "heat": {"comfort_heat": 71.0, "comfort_cool": 78.0, "sleep_cool": 76.0},
    "cool": {"comfort_cool": 75.0},
}
DAY_TARGET = {"heat": 71.0, "cool": 75.0}
LOOP_REASON = "remote-sensor offset"


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 3, hour, minute, second, tzinfo=UTC)


class CtxNight(Night):
    """A ``Night`` that starts at a chosen clock time and can attribute every setpoint write to its writer."""

    def __init__(self, start: datetime, fan_mode: str, direction: str = "heat", **kw) -> None:
        kw["config"] = {**DAY_DEFAULTS[direction], **(kw.get("config") or {})}
        previous = gates.NIGHT
        gates.NIGHT = start
        try:
            super().__init__(fan_mode, direction, **kw)
        finally:
            gates.NIGHT = previous
        self.day = DAY_TARGET[direction]
        self.sign = self.d["sign"]
        self.log: list[tuple[datetime, float, str]] = []  # (clock, comfort target passed in, reason) per CA write
        original = self.ae._set_temperature

        async def recording(target, *a, **k):
            self.log.append((self.sch.now(), target, str(k.get("reason", ""))))
            return await original(target, *a, **k)

        self.ae._set_temperature = recording

    # -- who wrote ----------------------------------------------------------------------------------------------
    def distinct_since(self, mark: int) -> list[float]:
        """The setpoint writes since ``mark`` with immediate repeats collapsed (a periodic same-value re-assert)."""
        out: list[float] = []
        for w in self.writes_since(mark):
            if not out or out[-1] != w:
                out.append(w)
        return out

    def loop_writes(self, since: datetime | None = None) -> list[tuple[datetime, float, str]]:
        return [e for e in self.log if LOOP_REASON in e[2] and (since is None or e[0] >= since)]

    # -- the clock ----------------------------------------------------------------------------------------------
    def run_to(self, hour: int, minute: int, second: int = 0, *, step_s: int = 10) -> None:
        end = at(hour, minute, second)
        assert self.sch.now() <= end, f"already past {end}: {self.sch.now()}"
        while self.sch.now() < end:
            self.wait(min(step_s, (end - self.sch.now()).total_seconds()))

    # -- a night -------------------------------------------------------------------------------------------------
    def to_cap(self) -> None:
        """CA wrote the sleep target; the loop has stepped the thermostat to the +4 / -4 cap (needs ~8 minutes)."""
        self.ca_writes()
        self.wait(61)
        self.wait(301)
        want = self.target + self.sign * 4.0
        assert self.sp == want, f"precondition: at the cap {want}, saw {self.sp}: {self.sp_writes()}"
        assert self.ae._remote_offset_applied_f == pytest.approx(4.0)

    def defer(self, name: str) -> object:
        """Replace an engine routine (bedtime / wake / classification) by a no-op; returns the original."""
        original = getattr(self.ae, name)

        async def nothing(*a, **k):
            return None

        setattr(self.ae, name, nothing)
        return original

    def restore(self, name: str, original) -> None:
        setattr(self.ae, name, original)

    def run_routine(self, original, *a) -> None:
        self.run(original(*a))

    def fail_setpoints(self) -> object:
        orig = self.fh.services.async_call

        async def failing(domain, service, data=None, *a, **k):
            if service == "set_temperature":
                raise RuntimeError("thermostat cloud unreachable")
            return await orig(domain, service, data, *a, **k)

        self.fh.services.async_call = failing
        return orig

    def heal_setpoints(self, orig) -> None:
        self.fh.services.async_call = orig

    def cold_bedroom(self) -> None:
        self.set_bedroom(self.d["off_target"])

    def set_bedroom_unavailable(self) -> None:
        with self.sch.installed():
            self.fh.states.async_set(BEDROOM, "unavailable", {})
            self.sch.advance_by(0)


def _night_before_wake(fan_mode: str, direction: str) -> CtxNight:
    """05:50, bedroom cold: at the cap by ~06:00 and quiet until the wake minute (the loop holds at the cap)."""
    n = CtxNight(at(5, 50), fan_mode, direction)
    n.to_cap()
    n.run_to(6, 29, 40)
    n.cold_bedroom()  # a fresh cold report just before wake: the loop would hold the cap, never step down
    return n


# ---------------------------------------------------------------------------------------------------------------------
# ENTRY
# ---------------------------------------------------------------------------------------------------------------------
ENTRY_BEDROOM = {"heat": 62.0, "cool": 80.0}  # far off BOTH the evening and the sleep target
IN_BAND_BEDROOM = {"heat": 70.5, "cool": 73.0}  # inside the hysteresis band of the evening target, no raise wanted


@pytest.mark.parametrize("direction", HEAT_AND_COOL)
@pytest.mark.parametrize("fan_mode", FAN_MODES)
class TestEntry:
    def _evening(self, fan_mode, direction) -> CtxNight:
        n = CtxNight(at(20, 28), fan_mode, direction, bedroom_f=ENTRY_BEDROOM[direction])
        n.ca_writes(n.day, reason="evening band")  # the evening comfort zone's last write
        return n

    def test_a_loop_check_before_the_bedtime_band_never_acts_on_the_evening_target(self, fan_mode, direction):
        """The sleep window opens and the loop gets there first: it must wait for the sleep band, not raise the
        EVENING number (which the bedtime band would then stack its own offset on)."""
        n = self._evening(fan_mode, direction)
        n.defer("handle_bedtime")
        n.defer("apply_classification")  # no other writer lands a band either
        n.run_to(20, 29, 50)
        mark = len(n.sp_writes())
        n.run_to(20, 31, 30)  # past 20:30: the sleep window is open, the loop has ticked, bedtime has not run
        assert n.ae._remote_context_key is not None, "precondition: the remote context began"
        assert n.writes_since(mark) == [], f"the loop acted on the evening target: {n.writes_since(mark)}"
        assert n.sp == n.day

    def test_the_bedtime_band_then_lands_plain_and_one_fresh_step_follows(self, fan_mode, direction):
        n = self._evening(fan_mode, direction)
        bedtime = n.defer("handle_bedtime")
        n.run_to(20, 31, 30)
        n.restore("handle_bedtime", bedtime)
        mark = len(n.sp_writes())
        n.run_routine(bedtime)  # the bedtime band
        assert n.writes_since(mark) == [n.target], f"the sleep band must land plain: {n.writes_since(mark)}"
        n.wait(61)  # the loop's next check: ONE fresh decision against the sleep target
        assert n.writes_since(mark)[-1] == n.one_step() and n.writes_since(mark)[0] == n.target, n.writes_since(mark)
        assert set(n.writes_since(mark)) <= {n.target, n.one_step()}, n.writes_since(mark)
        n.wait(250)
        assert len(n.loop_writes(at(20, 29))) == 1, "one fresh decision, no double write beside the bedtime band"

    def test_with_no_band_write_the_loop_decides_after_the_startup_wait(self, fan_mode, direction):
        """If the bedtime band never comes (every writer deferred) the loop must not stay silent all night."""
        n = self._evening(fan_mode, direction)
        n.defer("handle_bedtime")
        n.defer("apply_classification")  # no other writer lands a band either
        n.run_to(20, 29, 50)
        mark = len(n.sp_writes())
        n.run_to(20, 31, 30)
        assert n.loop_writes() == []
        n.wait(400)  # past the startup wait
        n.cold_bedroom()
        n.wait(70)
        decided = n.loop_writes()
        assert len(decided) == 1 and decided[0][1] == n.day, f"the loop never decided without a band write: {n.log}"
        assert n.sp == n.day + n.sign * 2.0, n.writes_since(mark)

    def test_a_sleep_context_entered_with_stale_loop_state_starts_clean(self, fan_mode, direction):
        """Left-over raise bookkeeping from an earlier context must not skip or delay the first decision."""
        n = CtxNight(at(20, 28), fan_mode, direction, bedroom_f=IN_BAND_BEDROOM[direction])
        n.ca_writes(n.day, reason="evening band")
        n.ae._remote_offset_f = 3.0
        n.ae._remote_raise_at = n.sch.now() - timedelta(seconds=5)
        n.ae._remote_raise_pending_feedback = True
        n.run_to(20, 31, 0)
        assert n.ae._remote_context_key is not None
        assert n.ae._remote_raise_pending_feedback is False
        assert n.ae._remote_raise_at is None or n.ae._remote_raise_at > at(20, 29), "a stale raise clock survived"
        assert n.ae._remote_offset_f == 0.0, n.ae._remote_offset_f  # fresh: no inherited step

    def test_the_applied_value_is_known_from_the_live_setpoint_after_a_restart_in_the_window(self, fan_mode, direction):
        """A restart left the thermostat at target +/- 4 with no memory: the first decision reads the live setpoint and
        brings it back, instead of trusting an unknown value."""
        n = CtxNight(at(20, 40), fan_mode, direction, bedroom_f=n_bed(direction))
        stale = n.target + n.sign * 4.0
        cs = n.fh.states.get(n.ce)
        n.fh.states.set(n.ce, gates.FakeState(state=cs.state, attributes={**cs.attributes, "temperature": stale}))
        n.ae._remote_target_f = n.target
        n.ae._remote_target_mode = n.d["mode"]
        n.ae._remote_target_context = None
        n.ae._remote_offset_applied_f = None
        n.set_bedroom(n.target)  # in band: the right setpoint is the plain target
        n.wait(400)
        assert n.sp == n.target, f"the stranded offset was not corrected: {n.sp_writes()}"


def n_bed(direction: str) -> float:
    return DIRECTIONS[direction]["off_target"]


# ---------------------------------------------------------------------------------------------------------------------
# EXIT
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("direction", HEAT_AND_COOL)
@pytest.mark.parametrize("fan_mode", FAN_MODES)
class TestExit:
    def test_the_wake_band_is_the_plain_morning_target_never_plus_the_offset(self, fan_mode, direction):
        n = _night_before_wake(fan_mode, direction)
        mark = len(n.sp_writes())
        n.run_to(6, 33, 0)  # the real wake routine at 06:30
        writes = n.writes_since(mark)
        assert writes, "precondition: the wake routine wrote the morning band"
        assert writes[-1] == n.day and n.sp == n.day, f"morning setpoint carried the bedroom offset: {writes}"
        assert all(w in (n.day, n.target) for w in writes), writes

    def test_the_loop_checking_first_at_exit_does_not_race_the_wake_band(self, fan_mode, direction):
        """The loop's check lands after the window closes but before the wake routine writes: it must leave the next
        band to its own writer (one write, the morning target), not write last night's target first."""
        n = _night_before_wake(fan_mode, direction)
        wake = n.defer("handle_morning_wakeup")
        mark = len(n.sp_writes())
        n.run_to(6, 31, 10)  # the window ended at 06:30:00; the loop has ticked; the wake routine has not run
        assert n.ae._remote_context_key is None, "precondition: the remote context ended"
        # The offset is cleared at the moment the context ends, before any next band is composed (the thermostat itself
        # still holds target +/- 4 until a writer lands: that is `applied`, not the loop's offset).
        assert n.ae._remote_offset_f == 0.0 and n.ae._remote_raise_at is None
        assert n.ae._remote_raise_pending_feedback is False
        assert n.ae._remote_offset_applied_f == pytest.approx(4.0)
        assert n.writes_since(mark) == [], f"the loop wrote ahead of the wake band: {n.writes_since(mark)}"
        n.restore("handle_morning_wakeup", wake)
        n.run_routine(wake, None)
        n.wait(300)
        assert n.distinct_since(mark) == [n.day], f"only the morning target: {n.writes_since(mark)}"
        assert n.loop_writes(at(6, 30)) == [], "the loop wrote beside the wake band"
        assert n.sp == n.day

    def test_with_no_wake_write_the_loop_writes_the_plain_target_once(self, fan_mode, direction):
        n = _night_before_wake(fan_mode, direction)
        n.defer("handle_morning_wakeup")  # wake skipped / moved: nothing else writes
        n.defer("apply_classification")
        mark = len(n.sp_writes())
        n.run_to(6, 30, 0)
        exit_at = n.sch.now()
        n.run_to(6, 31, 30)
        assert n.writes_since(mark) == [], "the loop must give the next band's writer a moment first"
        n.run_to(6, 33, 30)
        assert n.distinct_since(mark) == [n.target], f"one plain write: {n.writes_since(mark)}"
        loop = n.loop_writes(exit_at)
        assert len(loop) == 1 and (loop[0][0] - exit_at).total_seconds() >= REMOTE_OFFSET_EXIT_GRACE_S
        n.wait(900)
        assert n.distinct_since(mark) == [n.target], "exactly once"
        assert len(n.loop_writes(exit_at)) == 1
        assert n.sp == n.target

    def test_the_sensor_becoming_unconfigured_clears_the_offset(self, fan_mode, direction):
        n = _night_before_wake(fan_mode, direction)
        n.defer("handle_morning_wakeup")
        n.defer("apply_classification")
        mark = len(n.sp_writes())
        n.ae.config["sleep_indoor_temp_entity"] = None
        n.wait(60 * 5)
        assert n.distinct_since(mark) == [n.target], f"the offset stayed on the thermostat: {n.writes_since(mark)}"
        assert n.sp == n.target
        n.wait(600)
        assert n.distinct_since(mark) == [n.target]

    def test_the_sensor_going_unavailable_mid_night_clears_then_resumes(self, fan_mode, direction):
        """The bedroom sensor drops out at 02:00: plain target; when it is back the loop starts again from zero."""
        n = CtxNight(at(1, 50), fan_mode, direction)
        n.to_cap()
        n.set_bedroom_unavailable()
        mark = len(n.sp_writes())
        n.wait(60 * 6)
        assert n.distinct_since(mark) == [n.target], n.writes_since(mark)
        n.cold_bedroom()  # back, and cold
        n.wait(60 * 12)
        assert n.sp == n.target + n.sign * 2.0 or n.sp == n.target + n.sign * 4.0, n.sp_writes()
        assert n.ae._remote_offset_f <= 4.0 + 1e-6

    def test_a_moved_earlier_wake_time_ends_the_context_and_the_loop_clears_it(self, fan_mode, direction):
        """The configured wake time moved earlier while the offset was in force (the wake routine still fires at the
        old time): the loop itself writes the plain target once, then the old-time wake routine writes the morning
        band."""
        n = CtxNight(at(5, 50), fan_mode, direction)
        n.to_cap()
        n.run_to(6, 5, 0)
        n.cold_bedroom()
        n.defer("apply_classification")
        mark = len(n.sp_writes())
        n.ae.config["wake_time"] = "06:10"
        n.run_to(6, 10, 0)
        n.run_to(6, 11, 30)
        assert n.writes_since(mark) == [], "the loop must let a wake routine land first"
        n.run_to(6, 14, 30)
        assert n.distinct_since(mark) == [n.target], f"the loop's one plain write: {n.writes_since(mark)}"
        assert len(n.loop_writes(at(6, 10))) == 1
        n.run_to(6, 33, 0)
        assert n.distinct_since(mark) == [n.target, n.day], n.writes_since(mark)
        assert n.sp == n.day


# ---------------------------------------------------------------------------------------------------------------------
# EXIT while a gate holds the thermostat
# ---------------------------------------------------------------------------------------------------------------------
class _Gate:
    def __init__(self, name, close, release, closed_flag):
        self.name, self.close, self.release, self.closed = name, close, release, closed_flag


def _override(n: CtxNight) -> None:
    n.occupant_dials(n.target + n.sign * 3.0)
    n.wait(130)
    assert n.ae._manual_override_active, "precondition: the occupant's change was confirmed"


def _release_override(n: CtxNight) -> None:
    n.ae.cancel_override(reason="user_cancel_override")


def _nat_vent_on(n: CtxNight) -> None:
    n.ae._natural_vent_active = True


def _nat_vent_off(n: CtxNight) -> None:
    n.ae._natural_vent_active = False


def _away_on(n: CtxNight) -> None:
    n.ae._occupancy_mode = "away"


def _away_off(n: CtxNight) -> None:
    n.ae._occupancy_mode = "home"


def _pause_on(n: CtxNight) -> None:
    n.ae._paused_by_door = True


def _pause_off(n: CtxNight) -> None:
    n.ae._paused_by_door = False


GATES = {
    "override": (_override, _release_override),
    "nat_vent": (_nat_vent_on, _nat_vent_off),
    "away": (_away_on, _away_off),
    "door_pause": (_pause_on, _pause_off),
}


@pytest.mark.parametrize("gate", sorted(GATES))
@pytest.mark.parametrize("direction", HEAT_AND_COOL)
@pytest.mark.parametrize("fan_mode", FAN_MODES)
class TestExitDuringAGate:
    """The context ends while the occupant / a window / the fan / an away setback owns the thermostat: the owner keeps
    it, the offset is forgotten, and the loop writes nothing -- not at exit, and not later when the owner lets go."""

    def test_nothing_is_written_by_the_loop_after_the_context_ended(self, fan_mode, direction, gate):
        close, release = GATES[gate]
        n = CtxNight(at(5, 50), fan_mode, direction)
        n.to_cap()
        n.run_to(6, 20, 0)
        close(n)
        n.cold_bedroom()
        n.run_to(6, 29, 50)
        n.wait(0)
        exit_at = at(6, 30, 0)
        n.run_to(6, 45, 0)  # the context ended at 06:30 with the gate closed
        n.cold_bedroom()
        n.run_to(6, 50, 0)
        release(n)
        n.cold_bedroom()
        n.run_to(7, 20, 0)
        late = n.loop_writes(exit_at)
        assert late == [], f"the loop wrote after the context ended (gate {gate}): {late}"

    def test_the_offset_is_forgotten(self, fan_mode, direction, gate):
        close, _release = GATES[gate]
        n = CtxNight(at(5, 50), fan_mode, direction)
        n.to_cap()
        n.run_to(6, 20, 0)
        close(n)
        n.run_to(6, 34, 0)
        assert n.ae._remote_offset_f == 0.0
        assert n.ae._remote_raise_at is None and n.ae._remote_raise_pending_feedback is False
        assert n.ae._remote_offset_applied_f == 0.0, "the loop must not remember an offset it can no longer act on"


@pytest.mark.parametrize("direction", HEAT_AND_COOL)
@pytest.mark.parametrize("fan_mode", FAN_MODES)
class TestExitDuringAFreeze:
    def test_a_window_inside_its_debounce_at_exit_gets_no_loop_write_and_the_plain_morning_target(
        self, fan_mode, direction
    ):
        n = CtxNight(
            at(5, 50),
            fan_mode,
            direction,
            config={"sensor_debounce_seconds": 300},
            outdoor_f=gates.PAUSE_OUTDOOR_F[direction],
        )
        n.to_cap()
        n.run_to(6, 28, 30)
        n.window(True)  # inside its 300 s debounce across the wake minute
        mark = len(n.sp_writes())
        exit_at = at(6, 30, 0)
        n.run_to(6, 32, 30)
        assert n.ae._sensor_debounce_pending and not n.ae._paused_by_door, "precondition: still inside the debounce"
        assert n.loop_writes(exit_at) == [], f"the loop wrote inside the freeze: {n.loop_writes(exit_at)}"
        cap = n.target + n.sign * 4.0
        assert cap not in n.writes_since(mark), (
            f"the bedroom offset reached the morning setpoint: {n.writes_since(mark)}"
        )
        n.window(False)  # a blip: closes inside the debounce
        n.run_to(6, 45, 0)
        assert n.loop_writes(exit_at) == []
        assert n.sp == n.day, f"morning target expected, thermostat holds {n.sp}: {n.sp_writes()[mark:]}"


# ---------------------------------------------------------------------------------------------------------------------
# Restart (HA restarts mid-context and exactly at its boundaries)
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("direction", HEAT_AND_COOL)
@pytest.mark.parametrize("fan_mode", FAN_MODES)
class TestRestart:
    def _restarted(self, start, fan_mode, direction, *, stranded: bool = True) -> CtxNight:
        n = CtxNight(start, fan_mode, direction, skip_coalesce=False)
        if stranded:  # the old process left the thermostat at target +/- 4; this process remembers nothing
            cs = n.fh.states.get(n.ce)
            sp = n.target + n.sign * 4.0
            n.fh.states.set(n.ce, gates.FakeState(state=cs.state, attributes={**cs.attributes, "temperature": sp}))
        return n

    def test_restart_inside_the_context_writes_the_plain_sleep_target_then_resumes_quietly(self, fan_mode, direction):
        n = self._restarted(at(2, 0), fan_mode, direction)
        assert n.co._startup_coalesce_active
        mark = len(n.sp_writes())
        n.run(n.ae.apply_classification(n.ae._current_classification))  # CA's own startup classification
        assert set(n.writes_since(mark)) == {n.target}, (
            f"startup must write the plain sleep target: {n.writes_since(mark)}"
        )
        assert n.ae._remote_offset_applied_f == 0.0 and n.ae._remote_offset_f == 0.0
        n.cold_bedroom()
        n.wait(250)  # still inside the 300 s coalescing window: the loop adds nothing
        assert set(n.writes_since(mark)) == {n.target} and n.loop_writes() == []
        n.wait(200)
        n.cold_bedroom()
        n.wait(120)
        assert not n.co._startup_coalesce_active
        assert [t for _c, t, _r in n.loop_writes()] == [n.target], n.log
        assert n.sp == n.one_step(), n.sp_writes()[mark:]

    def test_restart_at_the_exit_boundary_writes_the_plain_morning_target(self, fan_mode, direction):
        n = self._restarted(at(6, 30, 0), fan_mode, direction)
        mark = len(n.sp_writes())
        n.run(n.ae.apply_classification(n.ae._current_classification))
        n.cold_bedroom()
        n.wait(600)
        cap = n.target + n.sign * 4.0
        assert cap not in n.writes_since(mark), n.writes_since(mark)
        assert n.sp == n.day, f"{n.sp} {n.writes_since(mark)}"
        assert n.loop_writes() == []

    def test_restart_one_minute_before_the_exit_boundary_hands_over_cleanly(self, fan_mode, direction):
        n = self._restarted(at(6, 29, 0), fan_mode, direction)
        mark = len(n.sp_writes())
        n.run(n.ae.apply_classification(n.ae._current_classification))
        assert n.sp == n.target, "startup: the plain sleep target, not target +/- 4"
        n.run_to(6, 40, 0)
        cap = n.target + n.sign * 4.0
        assert cap not in n.writes_since(mark), n.writes_since(mark)
        assert n.sp == n.day

    def test_restart_at_the_entry_boundary_starts_the_night_plain(self, fan_mode, direction):
        n = self._restarted(at(20, 30, 0), fan_mode, direction, stranded=False)
        mark = len(n.sp_writes())
        n.run(n.ae.apply_classification(n.ae._current_classification))
        assert n.writes_since(mark) == [n.target]
        assert n.ae._remote_offset_f == 0.0


# ---------------------------------------------------------------------------------------------------------------------
# The context ends during the failed-write back-off of increment (a)
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("direction", HEAT_AND_COOL)
@pytest.mark.parametrize("fan_mode", FAN_MODES)
class TestExitDuringBackOff:
    def _backed_off(self, fan_mode, direction):
        """+2 on the thermostat, then the cloud refuses the next raise at ~06:20 -> back-off until ~06:35."""
        n = CtxNight(at(6, 12), fan_mode, direction)
        n.ca_writes()
        n.wait(61)
        assert n.sp == n.one_step()
        healthy = n.fail_setpoints()
        n.run_to(6, 20, 0)
        n.cold_bedroom()
        n.run_to(6, 24, 0)
        assert n.ae._remote_write_failed_at is not None, "precondition: a loop write failed (back-off armed)"
        failed_at = n.ae._remote_write_failed_at
        return n, healthy, failed_at

    def test_a_wake_write_that_succeeds_ends_the_back_off_and_the_loop_stays_quiet(self, fan_mode, direction):
        n, healthy, _failed = self._backed_off(fan_mode, direction)
        n.heal_setpoints(healthy)
        mark = len(n.sp_writes())
        n.run_to(6, 40, 0)
        assert n.ae._remote_write_failed_at is None
        assert n.distinct_since(mark) == [n.day], f"only the morning band: {n.writes_since(mark)}"
        assert n.loop_writes(at(6, 30)) == []

    def test_with_no_wake_write_the_loop_waits_out_the_back_off_then_writes_once(self, fan_mode, direction):
        n, healthy, failed_at = self._backed_off(fan_mode, direction)
        n.defer("handle_morning_wakeup")
        n.defer("apply_classification")
        n.heal_setpoints(healthy)
        mark = len(n.sp_writes())
        while n.sch.now() < failed_at + timedelta(seconds=890):
            n.wait(10)
        assert n.ae._remote_context_key is None, "precondition: the context ended during the back-off"
        assert n.writes_since(mark) == [], "still backing off (15 minutes after the failure)"
        n.run_to(7, 10, 0)
        assert n.distinct_since(mark) == [n.target], f"one plain write: {n.writes_since(mark)}"
        wrote = n.loop_writes(at(6, 30))
        assert len(wrote) == 1 and (wrote[0][0] - failed_at).total_seconds() >= 900
        assert n.sp == n.target


# ---------------------------------------------------------------------------------------------------------------------
# Generic: any future context name works with the same entry/exit handling
# ---------------------------------------------------------------------------------------------------------------------
class TestAnyFutureContext:
    """The loop is keyed on ``IndoorTempReading.remote_context`` only. A hypothetical second context ("office") that
    supplies the reading outside the sleep window gets the same entry hold and exit clear, with no "sleep" word in the
    loop."""

    def _office_night(self, fan_mode="hvac_fan", direction="heat"):
        n = CtxNight(at(10, 0), fan_mode, direction, config={"sleep_indoor_temp_entity": BEDROOM})
        state = {"active": False}
        engine = n.ae
        real = engine._get_indoor_reading

        def reading() -> IndoorTempReading:
            base = real()
            if not state["active"]:
                return IndoorTempReading(base.primary_value, "sensor.hall", base.primary_value, None, None)
            value = float(n.fh.states.get(BEDROOM).state)
            return IndoorTempReading(value, BEDROOM, base.primary_value, None, None, "office")

        engine._get_indoor_reading = reading
        return n, state

    def test_entry_and_exit_work_for_a_context_that_is_not_sleep(self):
        n, state = self._office_night()
        n.defer("apply_classification")
        n.ca_writes(70.0, reason="day band")
        mark = len(n.sp_writes())
        n.cold_bedroom()
        n.wait(200)
        assert n.loop_writes() == [], "no remote context yet: no loop writes"
        state["active"] = True  # the office context begins
        n.ca_writes(68.0, reason="office band")
        assert n.ae._remote_context_key == f"office:{BEDROOM}"
        assert n.sp == 68.0 and n.ae._remote_offset_f == 0.0, "entry: plain band, offset 0"
        n.wait(61)
        assert n.sp == 70.0, f"one fresh decision after the band: {n.sp_writes()[mark:]}"
        n.wait(301)
        n.wait(150)
        assert n.sp == 72.0
        state["active"] = False  # the office context ends
        n.defer("handle_morning_wakeup")
        n.wait(60 * 4)
        assert n.sp == 68.0, f"exit: the loop wrote the plain target once: {n.sp_writes()[mark:]}"
        assert n.ae._remote_context_key is None

    def test_no_sleep_word_in_the_loop_state_machine(self):
        import ast
        import inspect
        import textwrap

        from custom_components.climate_advisor import automation

        for name in (
            "_sync_remote_context",
            "_enter_remote_context",
            "_exit_remote_context",
            "_forget_remote_offset",
            "_remote_awaiting_context_band",
        ):
            tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(automation.AutomationEngine, name))))
            for node in ast.walk(
                tree
            ):  # drop docstrings: the code and its identifiers/strings must carry no context name
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and ast.get_docstring(node):
                    node.body = node.body[1:] or [ast.Pass()]
            code = ast.unparse(tree).lower()
            assert "sleep" not in code and "wake" not in code, name


def test_exit_grace_is_the_existing_minimum_startup_wait():
    assert REMOTE_OFFSET_EXIT_GRACE_S == REMOTE_OFFSET_MIN_WAIT_S  # no new magic number


_ = (WINDOW, run_coro)  # imported for the rig re-exports used above


@pytest.mark.parametrize("direction", HEAT_AND_COOL)
@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_a_loop_check_while_the_bedtime_band_is_in_flight_sends_no_competing_value(fan_mode, direction):
    """The bedtime band's own write is still awaiting the (cloud) thermostat when a loop tick lands. Its target is
    already stamped for the new context, so without a guard the loop sent its +2 inside the band's call and the two
    values raced to the thermostat. The loop must leave it to the next check (strictly band first, then one step)."""
    n = CtxNight(at(20, 28), fan_mode, direction, bedroom_f=ENTRY_BEDROOM[direction])
    n.ca_writes(n.day, reason="evening band")
    n.defer("handle_bedtime")
    n.defer("apply_classification")
    n.run_to(20, 30, 5)
    orig = n.fh.services.async_call
    order: list[tuple[str, float | None]] = []
    nested = {"done": False}

    async def in_flight(domain, service, data=None, *a, **k):
        if service == "set_temperature" and not nested["done"]:
            nested["done"] = True
            order.append(("band", data.get("temperature")))
            await n.ae.remote_offset_check()  # a loop tick while the band's call is in flight
        elif service == "set_temperature":
            order.append(("competing", data.get("temperature")))
        return await orig(domain, service, data, *a, **k)

    n.fh.services.async_call = in_flight
    n.ca_writes(reason="bedtime band")
    n.fh.services.async_call = orig
    assert order == [("band", n.target)], f"a second value raced the bedtime band: {order}"
    n.wait(61)  # the next check makes the one fresh decision on what the band landed
    assert n.sp == n.one_step(), n.sp_writes()
