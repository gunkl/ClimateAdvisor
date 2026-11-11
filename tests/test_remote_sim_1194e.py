"""Issue #1194 increment (e): the simulation harness's remote "bedroom" node and its Tier A plumbing.

Occupant outcome: the hallway thermostat only sees the hallway, so on a cold night the person asleep in the bedroom
can be below their sleep temperature while the furnace never starts.  To prove Climate Advisor's outer loop fixes
that, the harness needs a bedroom that behaves like one: it lags behind the hallway, is cooled by outdoors, gets a
burst of register air on its sensor while the furnace runs, and reports on change with a ``last_reported`` that can
sit still for an hour.  These tests pin that plant (it is physics, not Climate Advisor logic), prove it is OPTIONAL
(every existing day, plan and serialised house is byte-for-byte what it was), and prove the new harness hooks
(``remote_sensor_report``, the delayed thermostat echo, ``commanded_setpoint_at`` / ``setpoint_writes``).
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tools.sim_harness import outcomes
from tools.sim_harness.fake_hass import FakeHass
from tools.sim_harness.fake_scheduler import FakeScheduler
from tools.sim_harness.generator import build_plan, strip_occupant_seconds
from tools.sim_harness.world import BedroomNode, BedroomParams, HouseParams, ToyHouse

# sha256 of json.dumps(build_plan(seed, profile).to_dict(), sort_keys=True) for seeds 0..59 x 3 profiles, taken on
# main (0.8.44) BEFORE the bedroom node existed.  The node is optional and draws nothing from the day's random
# stream, so every generated day must stay exactly what it was.
# Issue #1279 (0.8.50) moved the fast fan-then-HVAC press from 1-3 s to 3-5 s after the fan and re-took this digest;
# Issue #1288 moves it back to 1-3 s (owner decision: the rule-2 state backstop makes the press timing irrelevant).
# Same random draws, press time only, so the digest is again exactly the 0.8.44 value taken before the bedroom node.
_PLANS_DIGEST_BEFORE_BEDROOM = "7d35f57cef398e711dbdbdf4cc5699f54cf711edeb5095eb7ef8f0dc7c1ac3d0"


def test_every_generated_plan_is_byte_identical_without_the_bedroom_node() -> None:
    h = hashlib.sha256()
    for i in range(60):
        for profile in ("coordinator", "engine", "mixed"):
            # Issue #1527: the occupant's off-the-minute seconds are the plan's last draw; hash them on the minute
            h.update(json.dumps(strip_occupant_seconds(build_plan(i, profile).to_dict()), sort_keys=True).encode())
    assert h.hexdigest() == _PLANS_DIGEST_BEFORE_BEDROOM


def test_default_house_serialises_without_a_bedroom_key_and_round_trips() -> None:
    params = HouseParams(
        k_env_per_hr=0.1,
        solar_f_hr=1.0,
        heat_gain_f_hr=4.0,
        cool_gain_f_hr=4.0,
        swing_f=0.5,
        overshoot_f=0.2,
        whole_degree=True,
    )
    d = params.to_dict()
    assert "bedroom" not in d and "off_offset_f" not in d
    assert HouseParams(**d) == params
    with_node = HouseParams(**{**d, "bedroom": BedroomParams().__dict__})
    assert isinstance(with_node.bedroom, BedroomParams)
    assert HouseParams(**with_node.to_dict()) == with_node


def _house(bedroom: BedroomParams | None = None, **over) -> ToyHouse:
    params = HouseParams(
        k_env_per_hr=over.pop("k_env_per_hr", 0.05),
        solar_f_hr=0.0,
        heat_gain_f_hr=over.pop("heat_gain_f_hr", 6.0),
        cool_gain_f_hr=6.0,
        swing_f=1.5,
        overshoot_f=0.0,
        whole_degree=False,
        off_offset_f=-0.5,
        bedroom=bedroom,
    )
    return ToyHouse(params, over.pop("indoor", 68.0), over.pop("bedroom_f", None))


def test_the_hallway_physics_are_unchanged_by_the_bedroom() -> None:
    """With or without the node the hallway follows the same numbers (the node only listens to it)."""
    plain, two_zone = _house(None), _house(BedroomParams())
    for minute in range(120):
        plain.decide_action("heat", 70.0)
        two_zone.decide_action("heat", 70.0)
        plain.step(1, 45.0, 0.0, False)
        two_zone.step(1, 45.0, 0.0, False)
        assert plain.indoor_f == two_zone.indoor_f, minute
    assert plain.bedroom is None and two_zone.bedroom is not None


# ---------------------------------------------------------------------------------------------------------------
# The bedroom's physics: coupling, lag, plume, sensor
# ---------------------------------------------------------------------------------------------------------------
def test_coupling_steady_state_gap_below_the_hallway_follows_the_envelope_ratio() -> None:
    """dB/dt = kh (H - B) + ke (O - B): the room settles ke / (kh + ke) of the way from the hallway to outdoors."""
    p = BedroomParams(k_hall_per_hr=1.7, k_env_per_hr=0.37, sensor_step_c=0.0)
    node = BedroomNode(p, 66.0)
    for _ in range(12 * 60):
        node.step(1, 68.0, 50.0, None)
    expected = (1.7 * 68.0 + 0.37 * 50.0) / (1.7 + 0.37)
    assert node.room_f == pytest.approx(expected, abs=0.02)
    assert node.room_f < 68.0  # colder than the hallway: the room the thermostat cannot see


def test_lag_is_the_time_constant_one_over_the_total_coupling() -> None:
    """A 10 F step in the hallway reaches 63.2 % of the way after tau = 1 / (kh + ke) hours."""
    p = BedroomParams(k_hall_per_hr=1.2, k_env_per_hr=0.0, sensor_step_c=0.0)
    node = BedroomNode(p, 60.0)
    tau_min = 60.0 / 1.2
    node.step(tau_min, 70.0, 70.0, None)
    assert node.room_f == pytest.approx(60.0 + 10.0 * (1 - math.exp(-1)), abs=0.1)
    node.step(4 * tau_min, 70.0, 70.0, None)
    assert node.room_f == pytest.approx(70.0, abs=0.2)


def test_hallway_equipment_warms_the_bedroom_only_through_the_coupling() -> None:
    on, off = _house(BedroomParams(), indoor=68.0), _house(BedroomParams(), indoor=68.0)
    on.running = "heating"
    for _ in range(30):
        on.step(1, 45.0, 0.0, False)
        off.step(1, 45.0, 0.0, False)
    assert on.indoor_f > off.indoor_f + 2.0
    assert on.bedroom.room_f > off.bedroom.room_f + 0.8
    assert on.bedroom.room_f < on.indoor_f  # the room trails the hallway


def _plume_after(minutes_running: int, minutes_after: int = 0, running: str = "heating") -> tuple[float, float]:
    node = BedroomNode(BedroomParams(k_hall_per_hr=0.0, k_env_per_hr=0.0, sensor_step_c=0.0), 65.0)
    for _ in range(minutes_running):
        node.step(1, 65.0, 65.0, running)
    peak = node.plume_f
    for _ in range(minutes_after):
        node.step(1, 65.0, 65.0, None)
    return peak, node.plume_f


def test_plume_has_a_dead_time_then_rises_to_its_peak_on_the_sensor_not_the_room() -> None:
    p = BedroomParams()
    node = BedroomNode(BedroomParams(k_hall_per_hr=0.0, k_env_per_hr=0.0, sensor_step_c=0.0), 65.0)
    for minute in range(1, 5):
        node.step(1, 65.0, 65.0, "heating")
        assert node.plume_f == 0.0, f"minute {minute}: the air has not reached the sensor yet"
    seen = []
    for _ in range(40):
        node.step(1, 65.0, 65.0, "heating")
        seen.append(node.plume_f)
    assert all(b > a for a, b in zip(seen, seen[1:], strict=False)), "first-order rise toward the peak"
    assert seen[7] == pytest.approx(p.plume_f * (1 - math.exp(-8 / p.plume_rise_tau_min)), abs=0.2)
    assert seen[-1] == pytest.approx(p.plume_f, abs=0.15)
    assert node.room_f == pytest.approx(65.0)  # the plume is on the READING, never the room


def test_plume_coasts_during_the_blower_overrun_then_decays() -> None:
    peak, after_overrun = _plume_after(20, minutes_after=2)
    assert after_overrun > peak  # two more minutes of blower: still rising
    p = BedroomParams()
    _peak, late = _plume_after(20, minutes_after=2 + 11)
    assert late == pytest.approx(after_overrun * math.exp(-1), abs=0.25)  # one decay time constant later
    _peak, gone = _plume_after(20, minutes_after=2 + 90)
    assert abs(gone) < 0.05 * p.plume_f


def test_cooling_blows_a_cold_plume_on_the_sensor() -> None:
    peak, _ = _plume_after(25, running="cooling")
    assert peak < -3.0


def test_a_short_cycle_inside_the_dead_time_never_reaches_the_sensor() -> None:
    peak, _ = _plume_after(3)
    assert peak == 0.0


def test_sensor_reports_on_change_in_tenth_degree_celsius_steps_and_goes_silent() -> None:
    node = BedroomNode(BedroomParams(k_hall_per_hr=1.0, k_env_per_hr=0.0), 60.0)
    reports = 0
    for _ in range(90):
        reports += node.step(1, 70.0, 70.0, None)
        c = (node.reported_f - 32.0) / 1.8
        assert abs(c * 10 - round(c * 10)) < 1e-3, "reports sit on the 0.1 C grid"
    assert reports > 3 and node.age_min < 30
    node.silent = True
    frozen = node.reported_f
    node.step(130, 70.0, 70.0, None)
    assert node.reported_f == frozen and node.age_min >= 130, "a silent sensor keeps its last report and ages"


def test_a_steady_room_leaves_the_last_report_to_age_like_the_owners_sensor() -> None:
    node = BedroomNode(BedroomParams(), 65.0)
    node.step(240, 65.0, 65.0, None)
    assert node.age_min > 200  # nothing changed: no new report for hours (the 105-minute gaps the loop must survive)


# ---------------------------------------------------------------------------------------------------------------
# Harness hooks added with the node
# ---------------------------------------------------------------------------------------------------------------
def _result(action_log: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(action_log=action_log, event_log=[], engine_state={})


def _set_temp(ts: str, value: float) -> dict:
    return {
        "domain": "climate",
        "service": "set_temperature",
        "data": {"entity_id": "climate.x", "temperature": value},
        "ts": datetime.fromisoformat(ts).replace(tzinfo=UTC),
    }


LOG = [
    _set_temp("2026-11-02T20:30:00", 66.0),
    _set_temp("2026-11-02T20:31:00", 68.0),
    _set_temp("2026-11-02T20:36:00", 70.0),
]


@pytest.mark.parametrize(
    ("at", "value", "ok"),
    [
        ("2026-11-02T20:30:30", 66.0, True),
        ("2026-11-02T20:31:00", 68.0, True),  # a write AT the assertion time counts
        ("2026-11-02T21:00:00", 70.0, True),
        ("2026-11-02T21:00:00", 68.0, False),
        ("2026-11-02T20:00:00", 66.0, False),  # nothing written yet
    ],
)
def test_commanded_setpoint_at_reads_the_last_real_write(at: str, value: float, ok: bool) -> None:
    got = outcomes.check_assertion(_result(LOG), {"at": at, "expect": "commanded_setpoint_at", "value": value})
    assert (got == "commanded_setpoint_at") is ok


@pytest.mark.parametrize(
    ("since", "at", "values", "ok"),
    [
        ("2026-11-02T20:30:30", "2026-11-02T21:00:00", [68.0, 70.0], True),
        ("2026-11-02T20:30:30", "2026-11-02T21:00:00", [68.0], False),  # a missing write
        ("2026-11-02T20:30:30", "2026-11-02T21:00:00", [70.0, 68.0], False),  # wrong order
        ("2026-11-02T20:30:30", "2026-11-02T20:50:00", [68.0, 70.0, 72.0], False),  # an extra expected write
        (
            "2026-11-02T20:36:00",
            "2026-11-02T21:00:00",
            [],
            True,
        ),  # "since" is exclusive: an empty window means NO write
        ("2026-11-02T20:35:59", "2026-11-02T21:00:00", [], False),  # ... and a write in the window fails it
    ],
)
def test_setpoint_writes_is_an_exact_ordered_comparison(since: str, at: str, values: list[float], ok: bool) -> None:
    a = {"at": at, "expect": "setpoint_writes", "since": since, "values": values}
    assert (outcomes.check_assertion(_result(LOG), a) == "setpoint_writes") is ok


def test_delayed_thermostat_echo_holds_the_state_back_and_carries_no_context() -> None:
    sched = FakeScheduler(datetime(2026, 11, 2, 20, 0, tzinfo=UTC))
    hass = FakeHass(clock_fn=sched.now)
    hass.set_scheduler(sched)
    hass.states.set_simple("climate.t", "heat", {"temperature": 66.0})
    seen = []
    hass.add_state_listener("climate.t", lambda ev: seen.append(ev))

    async def write(context):
        await hass.services.async_call(
            "climate", "set_temperature", {"entity_id": "climate.t", "temperature": 68.0}, context=context
        )

    import asyncio

    # default: instant echo with the caller's context
    asyncio.run(write("ctx"))
    assert hass.states.get("climate.t").attributes["temperature"] == 68.0
    assert seen and seen[-1].context == "ctx"
    # opt-in delay: the command is logged now, the state (and its event) arrive later, without the context
    hass.set_echo_delay("climate.t", 90)
    seen.clear()
    asyncio.run(write("ctx2"))
    assert len(hass.action_log) == 2 and hass.states.get("climate.t").attributes["temperature"] == 68.0
    hass.states.set_simple("climate.t", "heat", {"temperature": 66.0})  # the thermostat still holds the old value
    seen.clear()
    sched.advance_to(sched.now() + timedelta(seconds=60))
    assert hass.states.get("climate.t").attributes["temperature"] == 66.0 and not seen
    sched.advance_to(sched.now() + timedelta(seconds=40))
    assert hass.states.get("climate.t").attributes["temperature"] == 68.0
    assert seen and seen[-1].context is None


# ---------------------------------------------------------------------------------------------------------------
# The closed-loop night through the real coordinator (tools/sim_harness/remote_night.py)
# ---------------------------------------------------------------------------------------------------------------
def _night(swing: float):
    from tools.sim_harness.remote_night import NightSpec, run_night

    return run_night(NightSpec(swing_f=swing))


@pytest.mark.parametrize("swing", [0.59, 1.0, 1.5])
def test_closed_loop_night_the_loop_gets_the_bedroom_heat_it_would_otherwise_never_get(
    swing: float, monkeypatch
) -> None:
    from custom_components.climate_advisor.automation import AutomationEngine

    with_loop = _night(swing)
    assert with_loop.callback_errors == 0
    assert with_loop.starts >= 1, "the furnace never started although the bedroom sat under the sleep floor"
    assert max(w for _, w in with_loop.writes) <= 70.0, "never more than target + 4 F"

    async def _no_check(self):
        return None

    monkeypatch.setattr(AutomationEngine, "remote_offset_check", _no_check)
    monkeypatch.setattr(AutomationEngine, "_remote_offset_for_write", lambda self, target_f, mode: 0.0)
    without = _night(swing)
    night_runs = [a for a, _ in without.runs if a.hour < 6 or (a.hour == 20 and a.minute >= 30) or a.hour > 20]
    assert not night_runs, "with the compensation deleted the hallway thermostat is satisfied all night"
    assert without.room_min_f < with_loop.room_min_f - 0.5, "the bedroom is colder without the loop"
    assert without.deficit_f_min > with_loop.deficit_f_min * 1.15, "and short of the sleep temperature for longer"


def test_closed_loop_night_is_deterministic() -> None:
    a, b = _night(1.5), _night(1.5)
    assert a.writes == b.writes and a.runs == b.runs
