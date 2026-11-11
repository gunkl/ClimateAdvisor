"""Cycle recorder state machine (Issues #1512 / #1516).

Occupant view: every real heating or cooling run, with the coast after it, is captured faithfully so the home's rates
sharpen with use; a Home Assistant restart or a thermostat that drops off the network never fakes an "off" edge (which
would teach the house a wrong, too-short run) and never glues two separate runs into one.
"""

from __future__ import annotations

import pytest

from custom_components.climate_advisor.const import (
    CYCLE_FLAG_FAN,
    CYCLE_FLAG_GAP_BEFORE,
    CYCLE_FLAG_NAT_VENT,
    CYCLE_FLAG_RUNNING,
    CYCLE_FLAG_UNAVAILABLE,
    CYCLE_FLAG_WINDOWS,
    CYCLE_POST_OFF_WINDOW_S,
)
from custom_components.climate_advisor.cycle_recorder import CycleRecorder

T0 = 1_768_000_000.0
SIGN = {"heat": 1.0, "cool": -1.0}
MODES = pytest.mark.parametrize("mode", ["heat", "cool"])


def _run(rec: CycleRecorder, mode: str, t_on: float, minutes: int = 6, start: float = 68.0):
    """On edge at t_on, a reading every minute moving 0.1 F/min in the mode's direction, off after ``minutes``."""
    closed = rec.on_edge(t_on, mode, start, 40.0, 70.0)
    for m in range(1, minutes + 1):
        closed += rec.reading(t_on + 60 * m, start + SIGN[mode] * 0.1 * m, "sensor.hall")
    closed += rec.off_edge(t_on + 60 * minutes, start + SIGN[mode] * 0.1 * minutes)
    return closed


@MODES
def test_on_off_then_window_closes_with_full_shape(mode: str) -> None:
    rec = CycleRecorder(True)
    assert _run(rec, mode, T0) == []
    assert rec.open_modes == [mode]
    # coast readings after off keep recording
    rec.reading(T0 + 360 + 120, 68.0 + SIGN[mode] * 0.62, "sensor.hall")
    closed = rec.tick(T0 + 360 + CYCLE_POST_OFF_WINDOW_S)
    assert len(closed) == 1
    c = closed[0]
    assert (c["mode"], c["t_on"], c["t_off"], c["cut"]) == (mode, T0, T0 + 360, None)
    assert c["id"] == f"{mode}-{int(T0)}"
    assert c["entity"] == "sensor.hall" and c["setpoint_f"] == 70.0
    assert c["outdoor"] == [[T0, 40.0]]
    assert c["res"] == pytest.approx(0.1)
    ts = [s[0] for s in c["samples"]]
    assert ts == sorted(ts) and ts[0] == T0
    flags = {s[0]: s[2] for s in c["samples"]}
    assert flags[T0] == CYCLE_FLAG_RUNNING
    assert flags[T0 + 300] == CYCLE_FLAG_RUNNING
    assert flags[T0 + 360] == 0  # the off-edge sample is not running
    assert flags[T0 + 480] == 0
    assert rec.open_modes == []


@MODES
def test_next_on_edge_closes_previous_cycle_and_starts_new(mode: str) -> None:
    rec = CycleRecorder(True)
    _run(rec, mode, T0)
    closed = rec.on_edge(T0 + 1200, mode, 68.3, 41.0, 70.0)
    assert [c["t_off"] for c in closed] == [T0 + 360]
    assert closed[0]["cut"] is None
    assert rec.open_modes == [mode] and rec.is_running(mode)


@MODES
def test_repeated_on_while_running_is_ignored(mode: str) -> None:
    rec = CycleRecorder(True)
    rec.on_edge(T0, mode, 68.0, 40.0, 70.0)
    assert rec.on_edge(T0 + 30, mode, 68.0, 40.0, 70.0) == []
    rec.off_edge(T0 + 300, 68.5)
    (c,) = rec.tick(T0 + 300 + CYCLE_POST_OFF_WINDOW_S)
    assert c["t_on"] == T0


@MODES
def test_off_without_a_running_cycle_is_ignored(mode: str) -> None:
    rec = CycleRecorder(True)
    assert rec.off_edge(T0, 68.0) == []
    _run(rec, mode, T0)
    assert rec.off_edge(T0 + 400, 68.7) == []  # already off: t_off must not move
    (c,) = rec.tick(T0 + 360 + CYCLE_POST_OFF_WINDOW_S)
    assert c["t_off"] == T0 + 360


def test_mode_switch_closes_the_other_mode_with_cut() -> None:
    rec = CycleRecorder(True)
    _run(rec, "heat", T0)
    closed = rec.on_edge(T0 + 700, "cool", 70.0, 90.0, 74.0)
    assert [(c["mode"], c["cut"]) for c in closed] == [("heat", "mode_switch")]
    assert rec.open_modes == ["cool"]


@MODES
def test_mode_switch_while_running_closes_with_no_off(mode: str) -> None:
    other = "cool" if mode == "heat" else "heat"
    rec = CycleRecorder(True)
    rec.on_edge(T0, mode, 68.0, 40.0, 70.0)
    (c,) = rec.on_edge(T0 + 120, other, 68.1, 40.0, 70.0)
    assert (c["mode"], c["t_off"], c["cut"]) == (mode, None, "mode_switch")


@MODES
def test_setpoint_change_closes_running_and_coasting_cycles(mode: str) -> None:
    rec = CycleRecorder(True)
    rec.on_edge(T0, mode, 68.0, 40.0, 70.0)
    (c,) = rec.setpoint_change(T0 + 100)
    assert (c["t_off"], c["cut"]) == (None, "setpoint_change")
    _run(rec, mode, T0 + 1000)
    (c2,) = rec.setpoint_change(T0 + 1000 + 360 + 600)
    assert (c2["t_off"], c2["cut"]) == (T0 + 1360, "setpoint_change")
    assert rec.setpoint_change(T0 + 99999) == []


@MODES
@pytest.mark.parametrize("reason", ["restart", "unavailable"])
def test_gap_is_not_an_off_edge_and_flags_next_samples(mode: str, reason: str) -> None:
    rec = CycleRecorder(True)
    rec.on_edge(T0, mode, 68.0, 40.0, 70.0)
    rec.reading(T0 + 60, 68.1 * 1.0 if mode == "heat" else 67.9, "sensor.hall")
    (c,) = rec.gap(T0 + 120, reason)
    assert c["t_off"] is None  # NOT an off edge
    assert c["cut"] == reason
    assert rec.open_modes == []
    # equipment still on after the gap: a new cycle starts, its first sample is flagged
    rec.on_edge(T0 + 600, mode, 69.0, 40.0, 70.0)
    rec.reading(T0 + 660, 69.1, "sensor.hall")
    rec.off_edge(T0 + 900, 69.5)
    (n,) = rec.tick(T0 + 900 + CYCLE_POST_OFF_WINDOW_S)
    first, second = n["samples"][0], n["samples"][1]
    want = CYCLE_FLAG_GAP_BEFORE | CYCLE_FLAG_RUNNING | (CYCLE_FLAG_UNAVAILABLE if reason == "unavailable" else 0)
    assert first[2] == want
    assert not second[2] & CYCLE_FLAG_GAP_BEFORE


def test_gap_after_off_keeps_the_coast_so_far() -> None:
    rec = CycleRecorder(True)
    _run(rec, "heat", T0)
    (c,) = rec.gap(T0 + 600, "restart")
    assert (c["t_off"], c["cut"]) == (T0 + 360, "restart")


def test_gap_with_nothing_open_returns_nothing_but_still_flags() -> None:
    rec = CycleRecorder(True)
    assert rec.gap(T0, "restart") == []
    rec.on_edge(T0 + 10, "heat", 68.0, 40.0, 70.0)
    rec.off_edge(T0 + 100, 68.2)
    (c,) = rec.tick(T0 + 100 + CYCLE_POST_OFF_WINDOW_S)
    assert c["samples"][0][2] & CYCLE_FLAG_GAP_BEFORE


def test_flags_are_stamped_per_sample_and_changes_get_a_time() -> None:
    rec = CycleRecorder(True)
    rec.on_edge(T0, "heat", 68.0, 40.0, 70.0)
    rec.reading(T0 + 60, 68.1, "sensor.hall")
    rec.flags(T0 + 90, windows=True, fan=False, nat_vent=False)
    rec.reading(T0 + 120, 68.2, "sensor.hall")
    rec.flags(T0 + 150, windows=False, fan=True, nat_vent=True)
    rec.off_edge(T0 + 200, 68.3)
    (c,) = rec.tick(T0 + 200 + CYCLE_POST_OFF_WINDOW_S)
    by_t = {s[0]: s for s in c["samples"]}
    assert by_t[T0 + 60][2] == CYCLE_FLAG_RUNNING
    assert by_t[T0 + 90][2] == CYCLE_FLAG_RUNNING | CYCLE_FLAG_WINDOWS
    assert by_t[T0 + 90][1] == 68.1  # the flag change re-states the last reading
    assert by_t[T0 + 120][2] == CYCLE_FLAG_RUNNING | CYCLE_FLAG_WINDOWS
    assert by_t[T0 + 150][2] == CYCLE_FLAG_RUNNING | CYCLE_FLAG_FAN | CYCLE_FLAG_NAT_VENT
    assert by_t[T0 + 200][2] == CYCLE_FLAG_FAN | CYCLE_FLAG_NAT_VENT


def test_every_change_kept_unchanged_value_skipped_seconds_preserved() -> None:
    rec = CycleRecorder(True)
    rec.on_edge(T0, "heat", 68.0, 40.0, 70.0)
    rec.reading(T0 + 17, 68.0, "sensor.hall")  # attribute-only update
    rec.reading(T0 + 43, 68.05, "sensor.hall")
    rec.reading(T0 + 43.5, 68.1, "sensor.hall")
    rec.reading(T0 + 20, 99.0, "sensor.hall")  # out of order: dropped
    rec.off_edge(T0 + 100, 68.1)
    (c,) = rec.tick(T0 + 100 + CYCLE_POST_OFF_WINDOW_S)
    assert [(s[0], s[1]) for s in c["samples"]] == [(T0, 68.0), (T0 + 43, 68.05), (T0 + 43.5, 68.1), (T0 + 100, 68.1)]


def test_outdoor_is_recorded_separately_and_deduplicated() -> None:
    rec = CycleRecorder(True)
    rec.on_edge(T0, "cool", 74.0, 90.0, 72.0)
    rec.outdoor(T0 + 60, 90.0)  # unchanged
    rec.outdoor(T0 + 120, 90.4)
    rec.off_edge(T0 + 200, 73.5)
    (c,) = rec.tick(T0 + 200 + CYCLE_POST_OFF_WINDOW_S)
    assert c["outdoor"] == [[T0, 90.0], [T0 + 120, 90.4]]


def test_only_one_entity_is_recorded_per_cycle() -> None:
    rec = CycleRecorder(True)
    rec.on_edge(T0, "heat", 68.0, 40.0, 70.0)
    rec.reading(T0 + 30, 68.1, "sensor.a")
    rec.reading(T0 + 31, 75.0, "sensor.b")
    rec.off_edge(T0 + 100, 68.3)
    (c,) = rec.tick(T0 + 100 + CYCLE_POST_OFF_WINDOW_S)
    assert c["entity"] == "sensor.a"
    assert all(s[1] < 70 for s in c["samples"])


def test_sample_cap_thins_interior_and_keeps_anchors() -> None:
    rec = CycleRecorder(True, max_samples=40)
    rec.on_edge(T0, "heat", 60.0, 40.0, 70.0)
    for i in range(1, 300):
        rec.reading(T0 + i * 10, 60.0 + i * 0.01, "sensor.hall")
    rec.off_edge(T0 + 3000, 63.1)
    rec.reading(T0 + 3010, 63.0, "sensor.hall")
    (c,) = rec.tick(T0 + 3000 + CYCLE_POST_OFF_WINDOW_S)
    assert len(c["samples"]) <= 41
    assert c["samples"][0][0] == T0
    assert (T0 + 3000) in [s[0] for s in c["samples"]]  # the off-edge sample survives
    ts = [s[0] for s in c["samples"]]
    assert ts == sorted(ts)
    assert c["thinned"] > 0
    assert CycleRecorder(True)._max_samples == 400  # production default (CYCLE_MAX_SAMPLES)


def test_reading_after_window_closes_the_cycle_first() -> None:
    rec = CycleRecorder(True)
    _run(rec, "heat", T0)
    closed = rec.reading(T0 + 360 + CYCLE_POST_OFF_WINDOW_S + 5, 70.0, "sensor.hall")
    assert len(closed) == 1
    assert closed[0]["samples"][-1][0] <= T0 + 360 + CYCLE_POST_OFF_WINDOW_S


def test_mode_capable_limits_modes_and_junk_is_ignored() -> None:
    rec = CycleRecorder(["heat"])
    assert rec.on_edge(T0, "cool", 70.0, 90.0, 74.0) == []
    assert rec.open_modes == []
    assert rec.on_edge(T0, "fan_only", 70.0) == []
    assert CycleRecorder(False).on_edge(T0, "heat", 70.0) == []
    rec.on_edge(T0, "heat", float("nan"), None, None)
    assert rec.reading(T0 + 1, None, "x") == [] and rec.reading(float("nan"), 70.0) == []
    rec.reading(T0 + 2, True, "x")  # type: ignore[arg-type]
    rec.off_edge(T0 + 30, None)
    (c,) = rec.tick(T0 + 30 + CYCLE_POST_OFF_WINDOW_S)
    assert c["samples"] == []
