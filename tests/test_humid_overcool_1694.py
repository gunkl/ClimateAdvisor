"""Issue #1694 (plan #1507 H3): humid overcool.

Occupant outcome: with the option on, on a muggy day a cooling run gets the thermostat set 1 F (then 2 F) below the
comfort target for at most 60 minutes, so the AC runs longer and dries the house; a dry day, the sleep window, an
open window, the occupant's own change and every other owner are left exactly as before. The comfort target and band
never move. These tests drive the pure decision, the one clamp and the REAL engine's one setpoint path; the
coordinator-mode goldens-to-be are ``tools/simulations/pending/humid_overcool_1694_*.json``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor import humid_overcool as ho
from custom_components.climate_advisor.const import (
    CONF_HUMID_OVERCOOL_ENABLED,
    CONFIG_METADATA,
    CYCLE_POST_OFF_WINDOW_S,
    DEFAULT_HUMID_OVERCOOL_ENABLED,
    OVERCOOL_RUN_CAP_S,
    OVERCOOL_RUN_END_GRACE_S,
    OVERCOOL_STEP_AFTER_S,
)
from custom_components.climate_advisor.engine_inputs import StaticEngineInputs
from custom_components.climate_advisor.humidity_facts import (
    LATCH_SPECS,
    OVERCOOL_HUMID_LATCH,
    RESERVED_EVENT_TYPES,
    SIGNAL_INDOOR,
    step_humid_latches,
)
from custom_components.climate_advisor.remote_offset import combine_adjustments, overcool_msg, remote_offset_view_for
from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_engine import build_headless_engine
from tools.sim_harness.fake_hass import FakeState

T0 = datetime(2026, 7, 12, 14, 0, 0, tzinfo=UTC)  # outside the 22:30-06:30 sleep window
ENTITY = "climate.test_thermostat"


def _inp(now=T0, **kw):
    args = {
        "now": now,
        "enabled": True,
        "observe_only": False,
        "gate": "open",
        "gate_reason": None,
        "cooling": True,
        "humid": True,
        "at_trigger": True,
        "headroom_f": 4.0,
    }
    args.update(kw)
    return ho.OvercoolInputs(**args)


def _active(engaged=T0, magnitude=1.0, **kw):
    kw.setdefault("lowered_since", engaged)
    return ho.OvercoolState(phase=ho.PHASE_ACTIVE, magnitude_f=magnitude, engaged_at=engaged, **kw)


# ── the pure decision ───────────────────────────────────────────────────────────────────────────────────────────────
class TestDecision:
    def test_engage_on_a_humid_cooling_run(self):
        d = ho.decide_overcool(ho.OvercoolState(), _inp())
        assert (d.action, d.write, d.event, d.state.magnitude_f) == (ho.ACTION_ENGAGE, True, ho.EVENT_STARTED, 1.0)
        assert d.state.phase == ho.PHASE_ACTIVE and d.state.engaged_at == T0

    @pytest.mark.parametrize(
        "kw",
        [{"cooling": False}, {"humid": False}, {"gate": "frozen"}, {"headroom_f": 0.5}, {"enabled": False}],
    )
    def test_no_engage(self, kw):
        d = ho.decide_overcool(ho.OvercoolState(), _inp(**kw))
        assert d.action == ho.ACTION_NONE and not d.write and d.state.magnitude_f == 0.0

    def test_observe_only_reports_once_per_run_and_changes_nothing(self):
        d = ho.decide_overcool(ho.OvercoolState(), _inp(observe_only=True))
        assert d.action == ho.ACTION_WOULD_ENGAGE and not d.write and d.state.phase == ho.PHASE_IDLE
        again = ho.decide_overcool(d.state, _inp(observe_only=True, now=T0 + timedelta(minutes=5)))
        assert again.action == ho.ACTION_NONE
        # the run ends (5 min not cooling): the next run reports again
        off = ho.decide_overcool(again.state, _inp(observe_only=True, cooling=False, now=T0 + timedelta(minutes=10)))
        ended = ho.decide_overcool(off.state, _inp(observe_only=True, cooling=False, now=T0 + timedelta(minutes=16)))
        assert not ended.state.observe_reported
        assert ho.decide_overcool(ended.state, _inp(observe_only=True)).action == ho.ACTION_WOULD_ENGAGE

    def test_second_step_after_30_minutes_only_while_at_the_trigger(self):
        st = _active()
        early = ho.decide_overcool(st, _inp(now=T0 + timedelta(seconds=OVERCOOL_STEP_AFTER_S - 1)))
        assert early.action == ho.ACTION_NONE
        at = T0 + timedelta(seconds=OVERCOOL_STEP_AFTER_S)
        stepped = ho.decide_overcool(st, _inp(now=at))
        assert (stepped.action, stepped.state.magnitude_f, stepped.write) == (ho.ACTION_STEP, 2.0, True)
        held = ho.decide_overcool(st, _inp(now=at, at_trigger=False))  # latch still on (59 F) but below 60 F
        assert held.action == ho.ACTION_NONE and held.state.magnitude_f == 1.0
        no_room = ho.decide_overcool(st, _inp(now=at, headroom_f=1.0))  # the gap rule allows only 1 F
        assert no_room.action == ho.ACTION_NONE
        never_past_max = ho.decide_overcool(stepped.state, _inp(now=at + timedelta(minutes=20)))
        assert never_past_max.action == ho.ACTION_NONE and never_past_max.state.magnitude_f == 2.0

    def test_cap_releases_at_60_minutes_and_no_reengage_this_run(self):
        st = _active(magnitude=2.0)
        d = ho.decide_overcool(st, _inp(now=T0 + timedelta(seconds=OVERCOOL_RUN_CAP_S)))
        assert (d.action, d.reason, d.write, d.event) == (ho.ACTION_RELEASE, ho.REASON_CAP, True, ho.EVENT_ENDED)
        assert d.state.phase == ho.PHASE_DONE and d.state.magnitude_f == 0.0
        assert ho.decide_overcool(d.state, _inp(now=T0 + timedelta(minutes=70))).action == ho.ACTION_NONE

    def test_dried_releases_at_once(self):
        d = ho.decide_overcool(_active(), _inp(now=T0 + timedelta(minutes=10), humid=False))
        assert (d.action, d.reason, d.write) == (ho.ACTION_RELEASE, ho.REASON_DRIED, True)

    def test_blip_shorter_than_the_grace_keeps_the_run(self):
        st = _active()
        off = ho.decide_overcool(st, _inp(cooling=False, now=T0 + timedelta(minutes=10)))
        assert off.action == ho.ACTION_NONE and off.state.not_cooling_since == T0 + timedelta(minutes=10)
        back = ho.decide_overcool(off.state, _inp(now=T0 + timedelta(minutes=13)))
        assert back.state.phase == ho.PHASE_ACTIVE and back.state.not_cooling_since is None

    def test_run_end_holds_then_restores_when_the_coast_closes(self):
        stop = T0 + timedelta(minutes=10)
        off = ho.decide_overcool(_active(), _inp(cooling=False, now=stop))
        ended = ho.decide_overcool(
            off.state, _inp(cooling=False, now=stop + timedelta(seconds=OVERCOOL_RUN_END_GRACE_S))
        )
        assert (ended.action, ended.reason, ended.write, ended.event) == (
            ho.ACTION_RUN_ENDED,
            ho.REASON_RUN_ENDED,
            False,
            ho.EVENT_ENDED,
        )
        assert ended.state.phase == ho.PHASE_COASTING and ended.state.magnitude_f == 1.0
        assert ended.state.stopped_at == stop
        hold = ho.decide_overcool(ended.state, _inp(cooling=False, now=stop + timedelta(minutes=30)))
        assert hold.action == ho.ACTION_NONE and hold.state.magnitude_f == 1.0
        done = ho.decide_overcool(
            ended.state, _inp(cooling=False, now=stop + timedelta(seconds=CYCLE_POST_OFF_WINDOW_S))
        )
        assert (done.action, done.reason, done.write) == (ho.ACTION_RESTORE, ho.REASON_COAST_OVER, True)
        assert done.state.phase == ho.PHASE_IDLE and done.state.magnitude_f == 0.0

    def test_next_cooling_start_restores_early_and_may_engage_the_new_run(self):
        coasting = ho.OvercoolState(phase=ho.PHASE_COASTING, magnitude_f=2.0, engaged_at=T0, stopped_at=T0)
        now = T0 + timedelta(minutes=20)
        dry = ho.decide_overcool(coasting, _inp(now=now, humid=False))
        assert (dry.action, dry.reason, dry.write, dry.state.magnitude_f) == (
            ho.ACTION_RESTORE,
            ho.REASON_NEXT_RUN,
            True,
            0.0,
        )
        humid = ho.decide_overcool(coasting, _inp(now=now))
        assert (humid.action, humid.reason, humid.event, humid.state.magnitude_f) == (
            ho.ACTION_ENGAGE,
            ho.REASON_NEXT_RUN,
            ho.EVENT_STARTED,
            1.0,
        )
        assert humid.state.engaged_at == now  # a new run, a new 60-minute budget

    @pytest.mark.parametrize("phase", [ho.PHASE_ACTIVE, ho.PHASE_COASTING])
    def test_gate_closed_zeroes_without_a_write(self, phase):
        st = ho.OvercoolState(phase=phase, magnitude_f=2.0, engaged_at=T0, stopped_at=T0)
        d = ho.decide_overcool(st, _inp(gate="closed", gate_reason="occupant_change"))
        assert d.action == ho.ACTION_ZERO and not d.write and d.state.magnitude_f == 0.0
        assert d.reason == "gate:occupant_change"
        # an active run is not re-engaged this run; a coasting one is simply over
        assert d.state.phase == (ho.PHASE_DONE if phase == ho.PHASE_ACTIVE else ho.PHASE_IDLE)
        assert d.event == (ho.EVENT_ENDED if phase == ho.PHASE_ACTIVE else None)

    def test_window_open_without_an_owner_asks_for_one_plain_write(self):
        d = ho.decide_overcool(_active(), _inp(gate="closed", gate_reason="window_open"))
        assert d.action == ho.ACTION_ZERO and d.write

    def test_gate_closed_while_idle_or_done(self):
        assert ho.decide_overcool(ho.OvercoolState(), _inp(gate="closed")).action == ho.ACTION_NONE
        done = ho.OvercoolState(phase=ho.PHASE_DONE, not_cooling_since=T0)
        later = ho.decide_overcool(done, _inp(gate="closed", cooling=False, now=T0 + timedelta(minutes=6)))
        assert later.state.phase == ho.PHASE_IDLE  # the run ended under the owner: the next run may engage
        assert ho.decide_overcool(_active(), _inp(gate="closed", gate_reason=None)).reason == "gate:other"

    def test_frozen_holds_everything(self):
        st = _active()
        d = ho.decide_overcool(st, _inp(gate="frozen", now=T0 + timedelta(seconds=OVERCOOL_RUN_CAP_S)))
        assert d.action == ho.ACTION_NONE and d.state.magnitude_f == 1.0

    def test_done_rearms_only_when_the_run_ends(self):
        done = ho.OvercoolState(phase=ho.PHASE_DONE)
        still = ho.decide_overcool(done, _inp())
        assert still.state.phase == ho.PHASE_DONE and still.action == ho.ACTION_NONE
        off = ho.decide_overcool(done, _inp(cooling=False))
        ended = ho.decide_overcool(off.state, _inp(cooling=False, now=T0 + timedelta(seconds=OVERCOOL_RUN_END_GRACE_S)))
        assert ended.state.phase == ho.PHASE_IDLE

    def test_option_off_forgets_everything(self):
        d = ho.decide_overcool(_active(magnitude=2.0), _inp(enabled=False))
        assert d.state == ho.OvercoolState() and not d.write

    def test_mixed_clocks_never_raise(self):
        naive = datetime(2026, 7, 12, 14, 0, 0)
        st = _active(engaged=naive, not_cooling_since=naive)
        assert ho.decide_overcool(st, _inp()).state.phase == ho.PHASE_ACTIVE
        assert ho.next_deadline_s(st, T0) is None


class TestDeadlines:
    def test_active(self):
        assert ho.next_deadline_s(_active(), T0 + timedelta(minutes=10)) == pytest.approx(20 * 60)
        assert ho.next_deadline_s(_active(magnitude=2.0), T0 + timedelta(minutes=40)) == pytest.approx(20 * 60)
        blip = _active(not_cooling_since=T0 + timedelta(minutes=10))
        assert ho.next_deadline_s(blip, T0 + timedelta(minutes=11)) == pytest.approx(4 * 60)

    def test_coasting_done_idle(self):
        coasting = ho.OvercoolState(phase=ho.PHASE_COASTING, magnitude_f=1.0, stopped_at=T0)
        assert ho.next_deadline_s(coasting, T0 + timedelta(minutes=5)) == pytest.approx(40 * 60)
        done = ho.OvercoolState(phase=ho.PHASE_DONE, not_cooling_since=T0)
        assert ho.next_deadline_s(done, T0) == pytest.approx(OVERCOOL_RUN_END_GRACE_S)
        assert ho.next_deadline_s(ho.OvercoolState(phase=ho.PHASE_DONE), T0) is None
        assert ho.next_deadline_s(ho.OvercoolState(), T0) is None
        assert ho.next_deadline_s(_active(), T0 + timedelta(hours=5)) == 1.0  # overdue: fire now


# ── the one clamp ───────────────────────────────────────────────────────────────────────────────────────────────────
class TestOneClamp:
    def test_remote_offset_alone_is_untouched(self):
        assert combine_adjustments("cool", 72.0, (("remote", -4.0),), floor=66.0, ceiling=72.0, gap_f=4.0) == (
            ("remote", -4.0),
        )

    def test_overcool_stops_at_the_gap_from_the_floor(self):
        out = combine_adjustments(
            "cool", 76.0, (("remote", 0.0), ("overcool", -2.0)), floor=68.0, ceiling=76.0, gap_f=4.0
        )
        assert out == (("remote", 0.0), ("overcool", -2.0))
        out = combine_adjustments(
            "cool", 74.0, (("remote", 0.0), ("overcool", -2.0)), floor=69.0, ceiling=74.0, gap_f=4.0
        )
        assert out == (("remote", 0.0), ("overcool", -1.0))  # 74 - 1 = 73 = floor + 4
        out = combine_adjustments(
            "cool", 76.0, (("remote", -2.0), ("overcool", -2.0)), floor=70.0, ceiling=76.0, gap_f=4.0
        )
        assert out == (("remote", -2.0), ("overcool", 0.0))  # the remote offset already uses the room

    def test_heat_mirror_and_wrong_sign(self):
        out = combine_adjustments("heat", 66.0, (("remote", 2.0), ("x", 3.0)), floor=66.0, ceiling=72.0, gap_f=4.0)
        assert out == (("remote", 2.0), ("x", 0.0))
        out = combine_adjustments("cool", 76.0, (("remote", 0.0), ("x", 1.0)), floor=68.0, ceiling=76.0, gap_f=4.0)
        assert out == (("remote", 0.0), ("x", 0.0))
        assert combine_adjustments("cool", 76.0, (), floor=68.0, ceiling=76.0, gap_f=4.0) == ()

    def test_headroom_is_the_same_clamp(self):
        assert ho.overcool_headroom_f(76.0, 68.0, 4.0) == pytest.approx(4.0)
        assert ho.overcool_headroom_f(76.0, 72.0, 4.0) == 0.0  # a 4 F band never overcools
        assert ho.overcool_headroom_f(76.0, 68.0, 4.0, already_f=3.0) == pytest.approx(1.0)


# ── option, latch, events ───────────────────────────────────────────────────────────────────────────────────────────
def test_option_is_off_by_default_and_has_metadata():
    assert DEFAULT_HUMID_OVERCOOL_ENABLED is False
    assert CONFIG_METADATA[CONF_HUMID_OVERCOOL_ENABLED]["category"] == "advanced"


def test_overcool_latch_60_on_58_off():
    spec = next(s for s in LATCH_SPECS if s.name == OVERCOOL_HUMID_LATCH)
    assert (spec.signal, spec.on, spec.off) == (SIGNAL_INDOOR, 60.0, 58.0)
    on = step_humid_latches({SIGNAL_INDOOR: 60.0}, {}, ceiling_f=None)
    assert on[OVERCOOL_HUMID_LATCH] is True
    held = step_humid_latches({SIGNAL_INDOOR: 58.5}, on, ceiling_f=None)
    assert held[OVERCOOL_HUMID_LATCH] is True
    assert step_humid_latches({SIGNAL_INDOOR: 58.0}, held, ceiling_f=None)[OVERCOOL_HUMID_LATCH] is False
    assert step_humid_latches({SIGNAL_INDOOR: 59.9}, {}, ceiling_f=None)[OVERCOOL_HUMID_LATCH] is False
    assert step_humid_latches({SIGNAL_INDOOR: None}, on, ceiling_f=None)[OVERCOOL_HUMID_LATCH] is False


def test_overcool_events_are_no_longer_reserved():
    assert "overcool_started" not in RESERVED_EVENT_TYPES and "overcool_ended" not in RESERVED_EVENT_TYPES


def test_activity_renderers():
    from custom_components.climate_advisor.ai_skills_context import EVENT_RENDERERS

    label, _ = EVENT_RENDERERS["overcool_started"](
        {"lowering_f": 1.0, "target": 76.0, "indoor_dew_point": 63.9}, "fahrenheit"
    )
    assert "76" in label and "63.9" in label
    assert EVENT_RENDERERS["overcool_started"]({"lowering_f": 1.0}, "fahrenheit")[0]
    assert EVENT_RENDERERS["overcool_started"]({"lowering_f": "x"}, "fahrenheit")[0]
    cap, _ = EVENT_RENDERERS["overcool_ended"]({"reason": "cap", "minutes": 60.0}, "fahrenheit")
    assert "60-minute" in cap and "60 min" in cap
    other, _ = EVENT_RENDERERS["overcool_ended"]({"reason": "gate:away", "minutes": "x"}, "fahrenheit")
    assert "another setting" in other


# ── the real engine: the one setpoint path ──────────────────────────────────────────────────────────────────────────
class Env:
    def __init__(self, *, enabled=True, comfort_heat=68.0, rh=64.0, indoor=77.0, action="cooling", start=T0, cfg=None):
        config = {
            "comfort_heat": comfort_heat,
            "comfort_cool": 76.0,
            "sleep_time": "22:30",
            "wake_time": "06:30",
            CONF_HUMID_OVERCOOL_ENABLED: enabled,
            "fan_mode": "disabled",
            "thermostat_hvac_modes": ["off", "heat", "cool"],
        }
        config.update(cfg or {})
        self.inputs = StaticEngineInputs(indoor_f=indoor, humidity=rh)
        self.engine, self.hass, self.sched, self.log = build_headless_engine(
            config=config,
            climate_entity=ENTITY,
            climate_state="cool",
            climate_attributes={"current_temperature": indoor, "temperature": 76.0, "hvac_action": action},
            start_time=start,
            inputs=self.inputs,
        )

    def set_action(self, action: str) -> None:
        cs = self.hass.states.get(ENTITY)
        attrs = dict(cs.attributes)
        attrs["hvac_action"] = action
        self.hass.states.set(ENTITY, FakeState(state=cs.state, attributes=attrs))

    def run(self, coro):
        with self.sched.installed():
            return run_coro(coro)

    def advance(self, seconds: float) -> None:
        with self.sched.installed():
            self.sched.advance_by(seconds)

    def write(self, target=76.0, mode="cool", **kw):
        return self.run(self.engine._set_temperature(target, reason="band", mode=mode, **kw))

    def check(self):
        return self.run(self.engine.overcool_check())

    def sp(self) -> list[float]:
        return [
            a["data"]["temperature"]
            for a in self.hass.action_log
            if a["domain"] == "climate" and a["service"] == "set_temperature"
        ]

    def events(self, name):
        return [p for (t, p, _ts) in self.log if t == name]


class TestEngine:
    def test_engage_step_and_cap_through_the_one_path(self):
        env = Env()
        env.write()
        env.check()
        assert env.sp() == [76.0, 75.0]
        assert env.events("overcool_started")[0]["lowering_f"] == 1.0
        assert env.engine._pending_setpoint_target == 76.0  # the comfort target never moves
        env.write()  # any other writer (the 30-minute band) re-sends the target: the term is re-applied
        assert env.sp()[-1] == 75.0
        env.advance(OVERCOOL_STEP_AFTER_S)  # the engine's own deadline timer steps it
        assert env.sp()[-1] == 74.0
        env.advance(OVERCOOL_RUN_CAP_S - OVERCOOL_STEP_AFTER_S)
        assert env.sp()[-1] == 76.0
        assert env.events("overcool_ended")[0]["reason"] == "cap"

    def test_option_off_is_byte_identical(self):
        env = Env(enabled=False)
        env.write()
        env.check()
        assert env.sp() == [76.0]
        assert env.engine._command_adjustments(76.0, "cool") == (("remote", 0.0),)
        assert env.engine.overcool_view() is None
        assert env.engine.remote_comfort_target_f() is None

    def test_tou_bank_and_heat_writes_never_carry_the_term(self):
        env = Env()
        env.write()
        env.check()
        env.write(70.0, skip_setpoint_sanity_check=True)  # a TOU bank write
        assert env.sp()[-1] == 70.0
        assert env.engine._overcool_for_write("heat") == 0.0

    def test_dry_house_and_no_cooling_report_do_nothing(self):
        dry = Env(rh=45.0)
        dry.write()
        dry.check()
        assert dry.sp() == [76.0]
        idle = Env(action="idle")
        idle.write()
        idle.check()
        assert idle.sp() == [76.0]

    def test_sleep_window_vetoes(self):
        env = Env(start=datetime(2026, 7, 12, 23, 0, 0, tzinfo=UTC))
        env.write()
        assert env.engine._overcool_gate_and_reason(datetime(2026, 7, 12, 23, 0, 0, tzinfo=UTC)) == (
            "closed",
            "sleep_window",
        )
        env.check()
        assert env.sp() == [76.0]

    def test_other_vetoes(self):
        env = Env()
        env.write()
        env.engine._grace_active = True
        assert env.engine._overcool_gate_and_reason(T0)[1] == "grace"
        env.engine._grace_active = False
        env.engine._tou_precondition_until = T0 + timedelta(hours=1)
        env.engine._tou_precondition_mode = "cool"
        assert env.engine._overcool_gate_and_reason(T0)[1] == "tou_bank"
        env.engine._tou_precondition_until = None
        env.engine._remote_target_mode = "heat"
        assert env.engine._overcool_gate_and_reason(T0)[1] == "mode"

    def test_gate_closing_on_a_write_zeroes_the_term(self):
        env = Env()
        env.write()
        env.check()
        env.engine._occupancy_mode = "away"  # away owns the thermostat
        env.write()
        assert env.sp()[-1] == 76.0
        assert env.events("overcool_ended")[-1]["reason"] == "gate:away"
        assert env.engine._overcool.phase == ho.PHASE_DONE

    def test_observe_only_logs_and_writes_nothing(self, caplog):
        env = Env()
        env.write()
        env.engine.dry_run = True
        with caplog.at_level("INFO"):
            env.check()
        # the write path's own dry-run line, with the lowering it would add (review W3A-B B6)
        assert "[DRY RUN] Would set temperature to 75" in caplog.text and "overcool=-1" in caplog.text
        assert env.sp() == [76.0] and not env.events("overcool_started")

    def test_narrow_band_gets_no_overcool(self):
        env = Env(comfort_heat=72.0)
        env.write()
        env.check()
        assert env.sp() == [76.0]

    def test_comfort_target_reader_and_views(self):
        env = Env()
        env.write()
        env.check()
        assert env.engine.remote_comfort_target_f() == 76.0  # P7 and the incident row judge the target
        view = env.engine.overcool_view()
        assert view["active"] and view["comfort_target_f"] == 76.0 and view["commanded_f"] == 75.0
        until, commanded = env.engine.overcool_commanded_window()
        assert commanded == 75.0 and until > T0
        status = remote_offset_view_for(env.engine, "fahrenheit")
        assert status["expected_setpoint"] == 75.0 and "75" in status["text"]
        assert status["overcool"]["active"] is True

    def test_adjustment_only_echo_and_own_setpoint(self):
        env = Env()
        env.write()
        env.check()
        assert env.engine.overcool_adjustment_echo(75.0, T0) is True  # only the overcool term changed
        assert env.engine.overcool_adjustment_echo(73.0, T0) is False
        assert env.engine.overcool_adjustment_echo(75.0, T0 + timedelta(minutes=4)) is False  # past the echo window
        assert env.engine.overcool_adjustment_echo(None, T0) is False
        env.write(73.0)  # a new comfort target: a real setpoint change, the echo memory is not renewed
        assert env.engine._overcool_echo[0] == 75.0
        env.engine._overcool_echo = None
        assert env.engine.overcool_adjustment_echo(74.5, T0) is False

    def test_off_cool_toggle_adoption_reads_cas_lowered_value_as_the_target(self):
        env = Env()
        env.engine._overcool_closed_commanded_f = 74.0
        assert env.engine._overcool_adopt_setpoint(74.0, 76.0, "cool") == 76.0
        assert env.engine._overcool_adopt_setpoint(72.0, 76.0, "cool") == 72.0  # the occupant's own choice
        assert env.engine._overcool_adopt_setpoint(74.0, 76.0, "heat") == 74.0
        env.engine._overcool_closed_commanded_f = None
        assert env.engine._overcool_adopt_setpoint(74.0, 76.0, "cool") == 74.0

    def test_restart_forgets_the_overcool(self):
        env = Env()
        env.write()
        env.check()
        env.engine.restore_state(env.engine.get_serializable_state())
        assert env.engine._overcool == ho.OvercoolState() and env.engine._overcool_applied_f == 0.0
        assert "_overcool" not in str(env.engine.get_serializable_state())  # never persisted

    def test_cleanup_cancels_the_deadline_timer(self):
        env = Env()
        env.write()
        env.check()
        assert env.engine._overcool_timer_cancel is not None
        env.engine.cleanup()
        assert env.engine._overcool_timer_cancel is None


def test_status_msg_variants():
    base = {"enabled": True, "active": True, "lowering_f": 2.0, "comfort_target_f": 76.0, "commanded_f": 74.0}
    assert overcool_msg({**base, "indoor_dew_point_f": 62.0}, "fahrenheit").key == "status.overcool.active"
    assert overcool_msg(base, "fahrenheit").key == "status.overcool.active_plain"
    assert overcool_msg({**base, "phase": "coasting"}, "fahrenheit").key == "status.overcool.holding"
    idle = {"enabled": True, "active": False}
    assert overcool_msg(idle, "fahrenheit") is None
    assert overcool_msg(idle, "fahrenheit", no_hvac_action=True).key == "status.overcool.no_hvac_action"
    assert overcool_msg({"enabled": False, "active": False}, "fahrenheit", no_hvac_action=True) is None


def test_status_view_without_overcool_is_unchanged():
    engine = SimpleNamespace(remote_offset_state=lambda: None, overcool_view=lambda: None)
    assert remote_offset_view_for(engine, "fahrenheit") is None
    warn = SimpleNamespace(remote_offset_state=lambda: None, overcool_view=lambda: {"enabled": True, "active": False})
    view = remote_offset_view_for(warn, "fahrenheit", no_hvac_action=True)
    assert view["overcool"]["warning"] == "no_hvac_action" and view["expected_setpoint"] is None
    broken = SimpleNamespace(remote_offset_state=lambda: None, overcool_view=lambda: 1 / 0)
    assert remote_offset_view_for(broken, "fahrenheit") is None


def test_api_reads_the_notice_only_with_the_option_on():
    from custom_components.climate_advisor.api import _thermostat_never_reports

    on = SimpleNamespace(config={CONF_HUMID_OVERCOOL_ENABLED: True}, thermostat_action_notice=lambda: {"text": "x"})
    assert _thermostat_never_reports(on) is True
    off = SimpleNamespace(config={}, thermostat_action_notice=lambda: {"text": "x"})
    assert _thermostat_never_reports(off) is False
    quiet = SimpleNamespace(config={CONF_HUMID_OVERCOOL_ENABLED: True}, thermostat_action_notice=lambda: None)
    assert _thermostat_never_reports(quiet) is False
    boom = SimpleNamespace(config={CONF_HUMID_OVERCOOL_ENABLED: True}, thermostat_action_notice=lambda: 1 / 0)
    assert _thermostat_never_reports(boom) is False


# ── coordinator pieces: chart, ODE, cycle recorder ──────────────────────────────────────────────────────────────────
def test_chart_target_band_and_ode_window():
    from custom_components.climate_advisor import coordinator as co

    active = SimpleNamespace(overcool_view=lambda: {"active": True, "comfort_target_f": 76.0})
    assert co._chart_overcool_target(active) == 76.0
    assert co._chart_overcool_target(SimpleNamespace(overcool_view=lambda: {"active": False})) is None
    assert co._chart_overcool_target(SimpleNamespace()) is None
    assert co._chart_overcool_target(SimpleNamespace(overcool_view=lambda: 1 / 0)) is None
    band = [{"ts": (T0 + timedelta(hours=h)).isoformat(), "lower": 68.0, "upper": 76.0} for h in range(0, 4)] + [
        {"ts": "bad", "lower": 68.0, "upper": 76.0}
    ]
    out = co._band_with_commanded_window(band, (T0 + timedelta(minutes=90), 74.0), T0)
    assert [b["upper"] for b in out] == [74.0, 74.0, 76.0, 76.0, 76.0]
    assert [b["upper"] for b in band] == [76.0] * 5  # the chart's own band is untouched
    assert co._band_with_commanded_window(band, None, T0) is band


def test_cycle_recorder_keeps_a_run_whole_across_an_overcool_step():
    """#1512: an adjustment-only setpoint change (comfort target unchanged) does not cut the cycle."""
    from custom_components.climate_advisor import coordinator as co
    from custom_components.climate_advisor.cycle_recorder import CycleRecorder

    coord = object.__new__(co.ClimateAdvisorCoordinator)
    coord.config = {"temp_unit": "fahrenheit", "learning_enabled": True, "climate_entity": ENTITY}
    coord._cycle_recorder = CycleRecorder(True)
    coord.automation_engine = SimpleNamespace(overcool_adjustment_echo=lambda sp: sp == 75.0)
    coord._last_outdoor_temp = 90.0
    coord._any_sensor_open = lambda: False
    coord._learning_indoor_f = lambda: 77.0
    coord._primary_indoor_entity = lambda: ENTITY
    coord._cycle_collect = lambda closed: None
    coord.automation_engine._fan_active = False
    coord.automation_engine._fan_override_active = False
    coord.automation_engine._natural_vent_active = False

    def st(sp, action):
        return SimpleNamespace(
            state="cool", attributes={"temperature": sp, "hvac_action": action, "current_temperature": 77.0}
        )

    coord._cycle_thermostat_event(st(76.0, "idle"), st(76.0, "cooling"), False, True)
    coord._cycle_thermostat_event(st(76.0, "cooling"), st(75.0, "cooling"), True, True)  # the overcool echo
    assert coord._cycle_recorder._open["cool"].get("_sp") is None  # not marked as a setpoint change
    coord._cycle_thermostat_event(st(75.0, "cooling"), st(72.0, "cooling"), True, True)  # the occupant's change
    assert coord._cycle_recorder._open["cool"].get("_sp") is not None
    coord.automation_engine = SimpleNamespace(overcool_adjustment_echo=lambda sp: 1 / 0)
    assert coord._cycle_overcool_echo(st(75.0, "cooling")) is False


def test_p1_skips_a_lowered_cool_setpoint():
    """Plan #1507 H3: P1 (settling flip) only judges a thermostat DEFENDING the edge; a cool setpoint below the
    ceiling (overcool) is not defending it, so no P1 finding can come from an overcool (verified, no code change)."""
    import inspect

    from custom_components.climate_advisor import invariant_watchdog as iw

    src = inspect.getsource(iw)
    assert "defending = cur.setpoint_f >= edge - NUMBERS.READING_SLACK_F" in src
    assert "if not (applies and defending):" in src


def test_status_view_with_a_remote_offset_keeps_its_sentence_and_adds_the_overcool():
    """Both adjustments at once (never in practice: D5 keeps overcool out of the sleep window): the loop's sentence
    stays, the overcool is described beside it and the thermostat is expected at the combined command."""
    from custom_components.climate_advisor.remote_offset import RemoteLoopState, RemoteOffsetState

    st = RemoteOffsetState(
        state=RemoteLoopState.ACTIVE,
        context="sleep",
        mode="cool",
        comfort_target_f=76.0,
        commanded_f=73.0,
        offset_f=-2.0,
        gate_reason=None,
        clamp_limit=None,
        last_reason="raised",
        remote_value_f=78.0,
        remote_age_s=30.0,
        cap_limit_f=4.0,
    )
    oc = {"enabled": True, "active": True, "lowering_f": 1.0, "comfort_target_f": 76.0, "commanded_f": 73.0}
    engine = SimpleNamespace(remote_offset_state=lambda: st, overcool_view=lambda: oc)
    view = remote_offset_view_for(engine, "fahrenheit", "en")
    plain = remote_offset_view_for(SimpleNamespace(remote_offset_state=lambda: st), "fahrenheit", "en")
    assert view["text"] == plain["text"]  # the loop's own sentence
    assert view["expected_setpoint"] == 73.0 and view["overcool"]["text"]
    idle = SimpleNamespace(remote_offset_state=lambda: st, overcool_view=lambda: {"enabled": True, "active": False})
    assert remote_offset_view_for(idle, "fahrenheit", "en") == plain  # nothing to add: exactly as before


# ── the coordinator-mode Tier-A scenarios (pending owner review) ────────────────────────────────────────────────────
_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
_PENDING = sorted((_ROOT / "tools" / "simulations" / "pending").glob("humid_overcool_1694_*.json"))


@pytest.mark.parametrize("scenario_path", _PENDING, ids=lambda p: p.stem.removeprefix("humid_overcool_1694_"))
def test_pending_humid_overcool_scenario_passes_production_engine(scenario_path):
    import json
    import sys

    sys.path.insert(0, str(_ROOT / "tools"))
    from simulate import run_scenario_production  # noqa: PLC0415

    assert json.loads(scenario_path.read_text(encoding="utf-8"))["use_coordinator"] is True
    result = run_scenario_production(scenario_path, state="pending")
    assert not result.get("callback_errors"), result.get("callback_errors")
    real = [x for x in result["assertions"] if not x.get("skipped")]
    assert real and len(real) == len(result["assertions"]), "no assertion of a humid-overcool scenario may be skipped"
    assert result["passed"] is True, "; ".join(
        f"@{x['at']} expected={x['expected']!r} actual={x['actual']!r}"
        for x in result["assertions"]
        if not x.get("pass")
    )


def test_humid_overcool_scenarios_discovered():
    assert len(_PENDING) == 17, [p.stem for p in _PENDING]


def test_api_helper_without_a_notice_reader():
    from custom_components.climate_advisor.api import _thermostat_never_reports

    assert _thermostat_never_reports(SimpleNamespace(config={CONF_HUMID_OVERCOOL_ENABLED: True})) is False


class TestEngineEdges:
    def test_pre_cool_and_freeze_gates(self):
        env = Env()
        env.write()
        env.engine._pre_cool_band_in_force_now = lambda: object()
        assert env.engine._overcool_gate_and_reason(T0) == ("closed", "pre_cool")
        del env.engine._pre_cool_band_in_force_now
        from custom_components.climate_advisor.remote_offset import RemoteOffsetGate

        env.engine._remote_offset_gate_and_reason = lambda: (RemoteOffsetGate.FREEZE, "debounce")
        assert env.engine._overcool_gate_and_reason(T0) == ("frozen", "debounce")
        env.engine._overcool_check_running = True
        env.check()  # re-entrancy guard
        assert env.sp() == [76.0]

    def test_check_is_inert_without_the_option_and_while_a_write_is_in_flight(self):
        off = Env(enabled=False)
        off.write()
        off.check()
        assert off.sp() == [76.0] and off.engine._overcool_timer_cancel is None
        busy = Env()
        busy.write()
        busy.engine._temp_command_pending = True
        busy.check()
        assert busy.sp() == [76.0]

    def test_headroom_without_a_target_and_echo_clock_mix(self):
        env = Env()
        assert env.engine._overcool_headroom() == 0.0  # nothing written yet
        env.engine._overcool_echo = (75.0, datetime(2026, 7, 12, 14, 0, 0))  # naive stamp
        assert env.engine.overcool_adjustment_echo(75.0, T0) is False

    def test_commanded_window_phases(self):
        env = Env()
        assert env.engine.overcool_commanded_window() is None
        env.write()
        env.check()
        env.engine._overcool = ho.OvercoolState(phase=ho.PHASE_COASTING, magnitude_f=1.0, stopped_at=T0)
        until, commanded = env.engine.overcool_commanded_window()
        assert until == T0 + timedelta(seconds=CYCLE_POST_OFF_WINDOW_S) and commanded == 75.0
        env.engine._overcool = ho.OvercoolState(phase=ho.PHASE_DONE)
        assert env.engine.overcool_commanded_window() is None

    def test_override_adoption_uses_the_mapping(self):
        env = Env()
        env.write()
        env.engine._overcool_closed_commanded_f = 74.0
        cs = env.hass.states.get(ENTITY)
        env.hass.states.set(ENTITY, FakeState(state="cool", attributes={**cs.attributes, "temperature": 74.0}))
        env.engine._manual_override_active = True
        env.engine._manual_override_mode = "cool"
        env.engine._manual_override_source = "normal"
        env.engine._override_decision_mode_and_target = lambda c: ("cool", 76.0)
        assert env.engine._override_matches_current_decision(SimpleNamespace(hvac_mode="cool")) is True
        env.engine._overcool_closed_commanded_f = None
        assert env.engine._override_matches_current_decision(SimpleNamespace(hvac_mode="cool")) is False


def test_coordinator_stub_safety():
    """Every coordinator overcool read tolerates a stub engine and a failing one (a listener never breaks)."""
    import asyncio

    from custom_components.climate_advisor import coordinator as co

    coord = object.__new__(co.ClimateAdvisorCoordinator)
    coord.config = {"temp_unit": "fahrenheit", CONF_HUMID_OVERCOOL_ENABLED: True}
    coord.automation_engine = SimpleNamespace()
    st = SimpleNamespace(state="cool", attributes={"temperature": 75.0})
    assert co._is_own_setpoint_echo(coord, st) is False
    assert coord._cycle_overcool_echo(st) is False
    assert coord._overcool_commanded_window() is None
    coord.automation_engine = SimpleNamespace(
        overcool_adjustment_echo=lambda sp: 1 / 0, overcool_commanded_window=lambda: 1 / 0
    )
    assert co._is_own_setpoint_echo(coord, st) is False
    assert coord._overcool_commanded_window() is None
    coord.automation_engine = SimpleNamespace(overcool_commanded_window=lambda: (T0, 74.0))
    assert coord._overcool_commanded_window() == (T0, 74.0)
    coord._shut_down = False
    coord._startup_coalesce_active = True
    asyncio.run(coord._async_overcool_tick())  # silent in the startup window
    coord._startup_coalesce_active = False
    coord.automation_engine = None
    asyncio.run(coord._async_overcool_tick())  # no engine: nothing to do

    calls = []

    @co._overcool_after_thermostat_event
    async def listener(self, event, **kw):
        calls.append(kw)

    class _Boom:
        config = {CONF_HUMID_OVERCOOL_ENABLED: True}

        async def _async_overcool_tick(self):
            raise RuntimeError("never breaks the listener")

    ev = SimpleNamespace(
        data={
            "old_state": SimpleNamespace(attributes={"hvac_action": "idle"}),
            "new_state": SimpleNamespace(attributes={"hvac_action": "cooling"}),
        }
    )
    asyncio.run(listener(_Boom(), ev))
    asyncio.run(listener(_Boom(), ev, recheck=True))
    assert len(calls) == 2


def test_band_window_mixed_clock_entry_is_left_alone():
    from custom_components.climate_advisor import coordinator as co

    band = [{"ts": "2026-07-12T14:30:00", "lower": 68.0, "upper": 76.0}]  # naive: cannot compare with an aware now
    assert co._band_with_commanded_window(band, (T0 + timedelta(hours=1), 74.0), T0)[0]["upper"] == 76.0


def test_briefing_and_ai_name_todays_overcooled_runs():
    """H5: the briefing's Humidity section names the AC runs humid overcool lengthened today (from the event log)."""
    import importlib

    from custom_components.climate_advisor import i18n
    from custom_components.climate_advisor.briefing import BriefingHumidity, _humidity_msg

    msg = _humidity_msg(BriefingHumidity(overcool_runs_today=2), "fahrenheit")
    assert "AC run longer 2 times to dry the air" in i18n.render_en(msg)
    assert _humidity_msg(BriefingHumidity(), "fahrenheit") is None
    coord_mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    coord = object.__new__(coord_mod.ClimateAdvisorCoordinator)
    coord._event_log = [
        {"time": "2026-07-12T13:00:00+00:00", "type": "overcool_started"},
        {"time": "2026-07-11T13:00:00+00:00", "type": "overcool_started"},  # yesterday
    ]
    assert coord.humid_decisions_today(T0)["overcool_runs"] == 1


# ── review W3A fixes ────────────────────────────────────────────────────────────────────────────────────────────────
class TestSpellBudget:
    """A2 (decision held for the owner): 60 lowered minutes per HUMID SPELL, shared by back-to-back runs."""

    def test_coast_re_engage_keeps_counting_and_a_spent_spell_does_not_re_engage(self):
        coasting = ho.OvercoolState(
            phase=ho.PHASE_COASTING, magnitude_f=1.0, engaged_at=T0, stopped_at=T0, lowered_since=T0
        )
        again = ho.decide_overcool(coasting, _inp(now=T0 + timedelta(minutes=20)))
        assert again.action == ho.ACTION_ENGAGE and again.state.lowered_since == T0  # the same stretch
        capped = ho.decide_overcool(again.state, _inp(now=T0 + timedelta(seconds=OVERCOOL_RUN_CAP_S)))
        assert (capped.action, capped.reason) == (ho.ACTION_RELEASE, ho.REASON_CAP)
        assert capped.state.spell_used_s == pytest.approx(OVERCOOL_RUN_CAP_S)
        # the run ends, the next run in the same spell is not overcooled
        off = ho.decide_overcool(capped.state, _inp(cooling=False, now=T0 + timedelta(minutes=61)))
        idle = ho.decide_overcool(off.state, _inp(cooling=False, now=T0 + timedelta(minutes=70)))
        assert idle.state.phase == ho.PHASE_IDLE
        assert ho.decide_overcool(idle.state, _inp(now=T0 + timedelta(minutes=80))).action == ho.ACTION_NONE
        # the latch releases (dry), then re-arms: a fresh budget
        dry = ho.decide_overcool(idle.state, _inp(cooling=False, humid=False, now=T0 + timedelta(minutes=90)))
        assert dry.state.spell_used_s == 0.0
        assert ho.decide_overcool(dry.state, _inp(now=T0 + timedelta(minutes=95))).action == ho.ACTION_ENGAGE

    def test_spent_while_coasting_restores_as_usual(self):
        st = ho.OvercoolState(
            phase=ho.PHASE_COASTING,
            magnitude_f=1.0,
            stopped_at=T0,
            lowered_since=T0 - timedelta(minutes=50),
        )
        late = T0 + timedelta(minutes=20)  # 70 lowered minutes: spent
        nxt = ho.decide_overcool(st, _inp(now=late))  # the next run starts: restore, no re-engage
        assert (nxt.action, nxt.reason, nxt.write) == (ho.ACTION_RESTORE, ho.REASON_NEXT_RUN, True)
        assert nxt.state.spell_used_s == pytest.approx(70 * 60)
        held = ho.decide_overcool(st, _inp(cooling=False, now=late))
        assert held.action == ho.ACTION_NONE  # the coast itself is not cut short

    def test_dried_release_resets_the_spell(self):
        d = ho.decide_overcool(_active(spell_used_s=600.0), _inp(humid=False, now=T0 + timedelta(minutes=5)))
        assert d.reason == ho.REASON_DRIED and d.state.spell_used_s == 0.0

    def test_cap_deadline_counts_the_spell(self):
        st = _active(engaged=T0, spell_used_s=40 * 60, magnitude=2.0)
        assert ho.next_deadline_s(st, T0) == pytest.approx(20 * 60)


class TestReleaseOnAutomationOff:
    """A1 and its mirror: switching automation off puts the plain target back with one write, then stays silent."""

    def test_overcool_lowering_is_released(self):
        env = Env()
        env.write()
        env.check()
        assert env.sp()[-1] == 75.0 and env.engine.command_adjustments_applied()
        env.run(env.engine.release_command_adjustments())
        assert env.sp()[-1] == 76.0
        assert not env.engine.command_adjustments_applied() and env.engine.overcool_view()["active"] is False
        env.engine.dry_run = True
        env.advance(OVERCOOL_STEP_AFTER_S)
        assert env.sp()[-1] == 76.0  # nothing re-engages while observe-only

    def test_nothing_applied_or_owner_holds_sends_nothing(self):
        env = Env()
        env.write()
        env.run(env.engine.release_command_adjustments())
        assert env.sp() == [76.0]
        env.check()
        env.engine._manual_override_active = True
        env.run(env.engine.release_command_adjustments())
        assert env.sp() == [76.0, 75.0]  # the occupant's change is theirs
        env.engine._manual_override_active = False
        env.engine.dry_run = True
        env.run(env.engine.release_command_adjustments())  # already observe-only: never writes

    def test_bedroom_offset_mirror_is_released_too(self):
        env = Env(enabled=False)
        env.write()
        env.engine._remote_offset_applied_f = 2.0  # the bedroom loop's offset is on the thermostat
        env.engine._remote_offset_f = 2.0
        env.run(env.engine.release_command_adjustments())
        assert env.sp()[-1] == 76.0 and env.engine._remote_offset_applied_f == 0.0
        assert env.engine._remote_offset_f == 0.0

    def test_coordinator_entry_releases_before_switching_off(self):
        import asyncio

        from custom_components.climate_advisor import coordinator as co

        calls = []

        class _Engine:
            def command_adjustments_applied(self):
                return True

            async def release_command_adjustments(self):
                calls.append(("release", coord._automation_enabled))

        coord = object.__new__(co.ClimateAdvisorCoordinator)
        coord._automation_enabled = True
        coord.automation_engine = _Engine()
        coord.set_automation_enabled = lambda enabled: calls.append(("set", enabled))
        asyncio.run(coord.async_set_automation_enabled(False))
        assert calls == [("release", True), ("set", False)]  # written while automation is still on (P3)
        calls.clear()
        asyncio.run(coord.async_set_automation_enabled(True))
        assert calls == [("set", True)]
        coord._automation_enabled = True
        coord.automation_engine = SimpleNamespace(command_adjustments_applied=lambda: 1 / 0)
        asyncio.run(coord.async_set_automation_enabled(False))  # never raises
        assert calls[-1] == ("set", False)
        stub = SimpleNamespace(set_automation_enabled=lambda enabled: calls.append(("stub", enabled)))
        asyncio.run(co.switch_automation(stub, True))
        assert calls[-1] == ("stub", True)


class TestEngineReviewFixes:
    def test_window_open_past_debounce_with_no_owner_writes_the_plain_target_once(self):
        from custom_components.climate_advisor.remote_offset import RemoteOffsetGate

        env = Env()
        env.write()
        env.check()
        assert env.sp()[-1] == 75.0
        env.engine._remote_offset_gate_and_reason = lambda: (RemoteOffsetGate.CLOSED, "window_open")
        env.check()
        assert env.sp()[-1] == 76.0 and env.events("overcool_ended")[-1]["reason"] == "gate:window_open"
        env.check()
        assert env.sp().count(76.0) == 2  # the band and that one restore: no further write

    def test_in_flight_write_rearms_a_short_retry(self):
        env = Env()
        env.write()
        env.engine._temp_command_pending = True
        env.check()
        assert env.engine._overcool_timer_cancel is not None  # re-checks in seconds, not at the next tick
        env.engine._temp_command_pending = False
        env.advance(30)
        assert env.sp()[-1] == 75.0

    def test_echo_memory_is_set_before_the_service_call(self):
        env = Env()
        env.write()
        seen = []

        async def _service(call):
            seen.append(env.engine._overcool_echo)

        env.hass.services.async_register("climate", "set_temperature", _service)
        env.check()
        assert seen and seen[-1] is not None and seen[-1][0] == 75.0

    def test_celsius_rounding_is_still_the_echo(self):
        env = Env(cfg={"temp_unit": "celsius"})
        env.engine._overcool_echo = (75.0, T0)  # 23.89 C commanded; a 0.5 C thermostat reports 24.0 C
        assert env.engine.overcool_adjustment_echo(75.2, T0) is True


# ── chart rows through the real coordinator ─────────────────────────────────────────────────────────────────────────
def _chart_rows(scenario: dict) -> list[dict]:
    from tools.sim_harness.run_production import FakeHassSession
    from tools.sim_harness.session import drive_scenario

    session = FakeHassSession(scenario, use_coordinator=True)
    try:
        drive_scenario(session, scenario)
        return list(session.coordinator._chart_log._entries)
    finally:
        session.close()


def test_each_overcool_step_echo_writes_one_setpoint_change_row():
    import json

    sc = json.loads((_ROOT / "tools/simulations/pending/humid_overcool_1694_engage_step_cap.json").read_text())
    rows = [r for r in _chart_rows(sc) if r.get("event") == "setpoint_change"]
    assert [r["setpoint"] for r in rows] == [75.0, 74.0, 76.0]  # engage, step, cap: read after the echo
    assert all(r.get("comfort_target") in (76.0, None) for r in rows)
    occupant = json.loads(
        (_ROOT / "tools/simulations/pending/humid_overcool_1694_occupant_change_kept.json").read_text()
    )
    assert [r["setpoint"] for r in _chart_rows(occupant) if r.get("event") == "setpoint_change"] == [75.0]


def test_option_off_bedroom_loop_chart_rows_and_logs_are_unchanged(caplog):
    """B1/A4: on a bedroom-sensor install with the option off, the loop's steps add no chart row and no log text."""
    import json

    path = _ROOT / "tools/simulations/pending/remote_offset_1194e_heat_night_cold_bedroom_raise_hold_step_down.json"
    sc = json.loads(path.read_text())
    assert sc["config"].get(CONF_HUMID_OVERCOOL_ENABLED) is None  # the default: Off
    with caplog.at_level("INFO"):
        rows = _chart_rows(sc)
    assert not [r for r in rows if r.get("event") == "setpoint_change"]
    assert "overcool" not in caplog.text
    assert "Set temperature to" in caplog.text  # the loop did write (its steps are the remote golden's own proof)


def test_ai_context_gets_its_own_overcool_line_and_the_dry_run_preview_is_cool_only():
    """B7: the AI investigator sees the overcool on its own line, never under the remote-sensor heading."""
    import asyncio
    from unittest.mock import MagicMock

    from custom_components.climate_advisor.ai_skills_context import build_hvac_entity_context

    env = Env()
    env.write()
    env.check()
    coord = MagicMock()
    coord.automation_engine = env.engine
    coord.config = {"temp_unit": "fahrenheit", "climate_entity": ENTITY}
    coord.data = {"hvac_mode": "cool"}
    hass = MagicMock()
    hass.states.get.return_value = MagicMock(attributes={"current_temperature": 77})
    ctx = asyncio.run(build_hvac_entity_context(hass, coord))
    assert "humid_overcool: phase='active'" in ctx and "remote_sensor_offset:" not in ctx
    env.engine.dry_run = True
    env.engine._overcool_preview_f = 1.0
    assert env.engine._overcool_for_write("heat") == 0.0  # a heat write never previews a lowering
    assert env.engine._overcool_for_write("cool") == -1.0
