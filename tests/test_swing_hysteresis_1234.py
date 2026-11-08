"""Issue #1234 - the learned swing must measure the thermostat's OWN hysteresis.

Occupant framing: on the owner's home the thermostat really starts heat about 1.4 F below its
setpoint, but CA learned 0.59 F (half the temperature rise per run, polluted by CA's own
setpoint jumps, phantom sessions and falling-temperature cycles). The dashboard then said
"Heat swing +/-0.6 F" and the AI flagged normal droop as a comfort shortfall; the planned
remote-sensor loop (#1194) would have cycled the equipment far tighter than the occupant set
their own thermostat. These tests drive the REAL coordinator thermostat listener with a real
LearningEngine and assert on what the occupant-facing thermal model reports.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor.indoor_temp import IndoorTempReading
from custom_components.climate_advisor.learning import LearningEngine
from tests.test_hvac_session_detection import _make_thermostat_coord

_T0 = datetime(2026, 11, 1, 14, 0, 0, tzinfo=UTC)
_FIXTURE = Path(__file__).parent / "fixtures" / "swing_hallway_cycles_1234.json"

# heat/cool mirror: (hvac mode, hvac_action while running, sign of "past the setpoint")
_MODES = {
    "heat": {"state": "heat", "running": "heating", "past": -1.0},
    "cool": {"state": "cool", "running": "cooling", "past": +1.0},
}


class _Rig:
    """A real thermostat listener + real LearningEngine, driven by state-change events."""

    def __init__(self, tmp_path: Path, mode: str, source: str = "sensor") -> None:
        self.mode = mode
        self.spec = _MODES[mode]
        self.coord = _make_thermostat_coord()
        self.coord.config["indoor_temp_source"] = "sensor" if source == "sensor" else "climate_fallback"
        self.coord.learning = LearningEngine(tmp_path)
        self.coord._async_run_temp_reactive_checks = AsyncMock()  # not under test
        # Setpoint moves here are CA's own commands, so override detection stays out of the way.
        self.coord._is_recent_temp_command = MagicMock(return_value=True)
        self._indoor = 70.0
        self.coord._get_indoor_temp_with_provenance = lambda: IndoorTempReading(self._indoor, "sensor.x", self._indoor)
        self.setpoint = 70.0
        self.action = "idle"
        self.state_mode = self.spec["state"]
        self.indoor_attr = 70.0

    def _state(self, action: str, setpoint: float, indoor_attr: float, state_mode: str) -> MagicMock:
        s = MagicMock()
        s.state = state_mode
        s.attributes = {
            "hvac_action": action,
            "temperature": setpoint,
            "current_temperature": indoor_attr,
            "fan_mode": "auto",
        }
        return s

    def fire(self, t: datetime, *, action=None, setpoint=None, indoor=None, state_mode=None) -> None:
        """Apply a change at time ``t``; ``indoor`` is the primary indoor reading at that instant."""
        old = self._state(self.action, self.setpoint, self.indoor_attr, self.state_mode)
        if action is not None:
            self.action = action
        if setpoint is not None:
            self.setpoint = setpoint
        if state_mode is not None:
            self.state_mode = state_mode
        if indoor is not None:
            self._indoor = indoor
            self.indoor_attr = indoor
        new = self._state(self.action, self.setpoint, self.indoor_attr, self.state_mode)
        event = MagicMock()
        event.data = {"old_state": old, "new_state": new}
        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = t
            asyncio.run(self.coord._async_thermostat_changed(event))

    def cycle(self, on_t: datetime, *, sp: float, on_indoor: float, off_indoor: float, minutes: float = 8.0) -> None:
        """One real cycle: idle -> running at ``on_t`` (steady setpoint), running -> idle later."""
        self.fire(on_t, action=self.spec["running"], setpoint=sp, indoor=on_indoor)
        self.fire(on_t + timedelta(minutes=minutes), action="idle", indoor=off_indoor)

    def model(self) -> dict:
        return self.coord.learning.get_thermal_model()

    @property
    def swing(self) -> float | None:
        return self.model()[f"swing_{self.mode}_f"]

    @property
    def raw(self) -> float | None:
        """Issue #1477: the median before the half-degree-up rounding (``swing`` is that rounded up)."""
        return self.model()[f"swing_{self.mode}_raw_f"]

    @property
    def count(self) -> int:
        return self.model()[f"observation_count_swing_{self.mode}"]


def _settled(rig: _Rig, sp: float) -> None:
    """Establish the starting setpoint long before the cycle under test."""
    rig.fire(_T0 - timedelta(hours=3), setpoint=sp, indoor=sp)


def _past(mode: str, sp: float, k: float) -> float:
    """Indoor reading that is ``k`` degrees past the setpoint in the equipment-start direction."""
    return sp + _MODES[mode]["past"] * k


@pytest.fixture(params=["heat", "cool"])
def mode(request) -> str:
    return request.param


# ---------------------------------------------------------------------------
# 1 / 2: a steady cycle is the textbook hysteresis sample
# ---------------------------------------------------------------------------


def test_steady_whole_degree_cycle_samples_the_quantised_hysteresis(tmp_path, mode):
    """Setpoint 69, equipment starts when the thermostat reads 68: swing = 1 (+/- 0.5)."""
    rig = _Rig(tmp_path, mode, source="climate")
    sp = 69.0 if mode == "heat" else 75.0
    _settled(rig, sp)
    on = _past(mode, sp, 1.0)
    rig.cycle(_T0, sp=sp, on_indoor=on, off_indoor=on)  # 68 -> 68: a flat whole-degree cycle
    assert rig.swing == pytest.approx(1.0)
    assert rig.count == 1
    assert rig.model()[f"swing_{mode}_resolution_f"] == pytest.approx(1.0)


def test_steady_fine_sensor_cycle_samples_the_real_hysteresis(tmp_path, mode):
    """The 0.18 F hallway sensor sees the true start point: 1.36 below the setpoint."""
    rig = _Rig(tmp_path, mode, source="sensor")
    sp = 69.0 if mode == "heat" else 75.0
    _settled(rig, sp)
    on = _past(mode, sp, 1.36)
    off = _past(mode, sp, -0.18)  # coasts to just past the setpoint
    rig.cycle(_T0, sp=sp, on_indoor=on, off_indoor=off)
    assert rig.swing == 1.5 and rig.raw == pytest.approx(1.36)  # Issue #1477: stored = raw rounded up to 0.5
    assert rig.model()[f"swing_{mode}_resolution_f"] == 0.0


# ---------------------------------------------------------------------------
# 3: setpoint-change window (both sides of the boundary)
# ---------------------------------------------------------------------------


def _valid_then(rig: _Rig, sp: float) -> None:
    """A positive control: one valid steady cycle so exclusions are measured against a baseline."""
    _settled(rig, sp)
    on = _past(rig.mode, sp, 1.4)
    rig.cycle(_T0 - timedelta(hours=1), sp=sp, on_indoor=on, off_indoor=_past(rig.mode, sp, 0.2))
    assert rig.count == 1


def test_on_transition_inside_setpoint_change_window_is_not_hysteresis(tmp_path, mode):
    """CA (or the occupant) jumped the setpoint 30 s ago: the equipment start is the jump."""
    rig = _Rig(tmp_path, mode)
    sp = 69.0 if mode == "heat" else 75.0
    _valid_then(rig, sp)
    new_sp = sp + (4.0 if mode == "heat" else -4.0)  # a 4 F jump -> start 4 F "past" the new setpoint
    rig.fire(_T0, setpoint=new_sp, indoor=sp)
    rig.fire(_T0 + timedelta(seconds=30), action=_MODES[mode]["running"], indoor=sp)
    rig.fire(_T0 + timedelta(minutes=9), action="idle", indoor=_past(mode, new_sp, 0.5))
    assert rig.count == 1
    assert rig.swing == 1.5 and rig.raw == pytest.approx(1.4)  # Issue #1477: stored = raw rounded up to 0.5


def test_setpoint_change_window_boundary_both_sides(tmp_path, mode):
    """Just inside the window is a jump; 2 min + epsilon after the change IS a sample."""
    from custom_components.climate_advisor.const import THERMAL_SWING_SETPOINT_CHANGE_WINDOW_S as W

    rig = _Rig(tmp_path, mode)
    sp = 69.0 if mode == "heat" else 75.0
    _settled(rig, sp)
    # inside: W - 1 s after a setpoint change -> excluded
    rig.fire(_T0, setpoint=sp + 1.0, indoor=sp)
    rig.fire(_T0 + timedelta(seconds=W - 1), action=_MODES[mode]["running"], indoor=_past(mode, sp + 1.0, 2.0))
    rig.fire(_T0 + timedelta(minutes=9), action="idle", indoor=_past(mode, sp + 1.0, 0.5))
    assert rig.count == 0
    # outside: W + 1 s after the next change -> sampled
    t1 = _T0 + timedelta(hours=1)
    rig.fire(t1, setpoint=sp, indoor=sp)
    rig.fire(t1 + timedelta(seconds=W + 1), action=_MODES[mode]["running"], indoor=_past(mode, sp, 1.5))
    rig.fire(t1 + timedelta(minutes=9), action="idle", indoor=_past(mode, sp, 0.3))
    assert rig.count == 1
    assert rig.swing == pytest.approx(1.5)


def test_window_boundary_exact_length_is_sampled_for_setpoint_and_mode_changes(tmp_path, mode):
    """Exactly W s after a setpoint OR hvac-mode change is a sample; W - 1 s after a mode change is not."""
    from custom_components.climate_advisor.const import THERMAL_SWING_SETPOINT_CHANGE_WINDOW_S as W

    rig = _Rig(tmp_path, mode)
    sp = 69.0 if mode == "heat" else 75.0
    _settled(rig, sp)
    rig.fire(_T0, setpoint=sp + 0.5, indoor=sp)
    rig.fire(_T0, setpoint=sp, indoor=sp)
    rig.fire(_T0 + timedelta(seconds=W), action=_MODES[mode]["running"], indoor=_past(mode, sp, 1.4))
    rig.fire(_T0 + timedelta(minutes=9), action="idle", indoor=_past(mode, sp, 0.5))
    assert rig.count == 1
    # mode change off -> heat/cool, start W - 1 s later: a jump
    t1 = _T0 + timedelta(hours=1)
    rig.fire(t1 - timedelta(minutes=30), state_mode="off", indoor=_past(mode, sp, 2.0))
    rig.fire(t1, state_mode=_MODES[mode]["state"], indoor=_past(mode, sp, 2.0))
    rig.fire(t1 + timedelta(seconds=W - 1), action=_MODES[mode]["running"], indoor=_past(mode, sp, 2.0))
    rig.fire(t1 + timedelta(minutes=9), action="idle", indoor=_past(mode, sp, 0.5))
    assert rig.count == 1
    # mode change, start exactly W s later: a sample
    t2 = _T0 + timedelta(hours=2)
    rig.fire(t2 - timedelta(minutes=30), state_mode="off", indoor=_past(mode, sp, 1.6))
    rig.fire(t2, state_mode=_MODES[mode]["state"], indoor=_past(mode, sp, 1.6))
    rig.fire(t2 + timedelta(seconds=W), action=_MODES[mode]["running"], indoor=_past(mode, sp, 1.6))
    rig.fire(t2 + timedelta(minutes=9), action="idle", indoor=_past(mode, sp, 0.5))
    assert rig.count == 2
    assert rig.swing == pytest.approx(1.5)


def test_hvac_mode_change_counts_as_a_jump_not_hysteresis(tmp_path, mode):
    """Switching off->heat/cool starts the equipment at once from wherever the room is."""
    rig = _Rig(tmp_path, mode)
    sp = 69.0 if mode == "heat" else 75.0
    _valid_then(rig, sp)
    rig.fire(_T0 - timedelta(minutes=30), state_mode="off", indoor=_past(mode, sp, 3.0))
    rig.fire(_T0, state_mode=_MODES[mode]["state"], indoor=_past(mode, sp, 3.0))
    rig.fire(_T0 + timedelta(seconds=20), action=_MODES[mode]["running"], indoor=_past(mode, sp, 3.0))
    rig.fire(_T0 + timedelta(minutes=9), action="idle", indoor=_past(mode, sp, 0.2))
    assert rig.count == 1
    assert rig.swing == 1.5 and rig.raw == pytest.approx(1.4)  # Issue #1477: stored = raw rounded up to 0.5


# ---------------------------------------------------------------------------
# 4: wrong-direction run
# ---------------------------------------------------------------------------


def test_wrong_direction_run_gives_no_sample(tmp_path, mode):
    """A heat cycle whose temperature FELL (cool: ROSE) is a measurement artefact, not a sample."""
    rig = _Rig(tmp_path, mode)
    sp = 69.0 if mode == "heat" else 75.0
    _valid_then(rig, sp)
    on = _past(mode, sp, 1.3)
    rig.cycle(_T0, sp=sp, on_indoor=on, off_indoor=on + _MODES[mode]["past"] * 0.4)  # moved further away
    assert rig.count == 1
    assert rig.swing == 1.5 and rig.raw == pytest.approx(1.4)  # Issue #1477: stored = raw rounded up to 0.5


def test_on_at_or_inside_setpoint_gives_no_sample(tmp_path, mode):
    """Equipment that starts at the setpoint (value <= 0) tells us nothing about hysteresis."""
    rig = _Rig(tmp_path, mode)
    sp = 69.0 if mode == "heat" else 75.0
    _valid_then(rig, sp)
    rig.cycle(_T0, sp=sp, on_indoor=sp, off_indoor=_past(mode, sp, -1.0))
    rig.cycle(_T0 + timedelta(hours=1), sp=sp, on_indoor=_past(mode, sp, -0.5), off_indoor=_past(mode, sp, -1.5))
    assert rig.count == 1


# ---------------------------------------------------------------------------
# 5: phantom (mode-fallback) sessions
# ---------------------------------------------------------------------------


def test_phantom_mode_fallback_session_starts_no_observation_and_no_sample(tmp_path, mode):
    """hvac_mode off->heat while hvac_action stays idle is NOT a run of the equipment."""
    rig = _Rig(tmp_path, mode)
    sp = 69.0 if mode == "heat" else 75.0
    rig.fire(_T0 - timedelta(hours=3), setpoint=sp, indoor=sp, state_mode="off")
    rig.fire(_T0, state_mode=_MODES[mode]["state"], indoor=_past(mode, sp, 1.4))  # action stays idle
    rig.fire(_T0 + timedelta(minutes=9), state_mode="off", indoor=sp)
    rig.coord._start_hvac_observation.assert_not_called()
    assert rig.count == 0


def test_real_run_after_a_phantom_session_still_gets_its_observation(tmp_path, mode):
    """The first real hvac_action edge after a phantom mode change starts the observation."""
    rig = _Rig(tmp_path, mode)
    sp = 69.0 if mode == "heat" else 75.0
    rig.fire(_T0 - timedelta(hours=3), setpoint=sp, indoor=sp, state_mode="off")
    rig.fire(_T0 - timedelta(minutes=30), state_mode=_MODES[mode]["state"], indoor=sp)  # phantom
    rig.coord._start_hvac_observation.reset_mock()
    rig.fire(_T0, action=_MODES[mode]["running"], indoor=_past(mode, sp, 1.4))
    rig.coord._start_hvac_observation.assert_called_once_with(mode)


# ---------------------------------------------------------------------------
# 8: real measured cycles
# ---------------------------------------------------------------------------


def _drive_fixture(tmp_path: Path, *, key_on: str, key_off: str, source: str) -> _Rig:
    rig = _Rig(tmp_path, "heat", source=source)
    cycles = json.loads(_FIXTURE.read_text())["cycles"]
    prev_sp = None
    for c in cycles:
        on_t = datetime.fromisoformat(c["on_utc"]).replace(tzinfo=UTC)
        sp = c["setpoint_f"]
        age = c["setpoint_age_min"]
        if prev_sp is None:
            rig.fire(on_t - timedelta(hours=2), setpoint=sp, indoor=sp)
        if age < 30 or sp != prev_sp:
            # setpoint last changed `age` minutes before the on-transition
            change_t = on_t - timedelta(minutes=max(age, 0.2))
            rig.fire(change_t, setpoint=sp, indoor=c[key_on])
        rig.fire(on_t, action="heating", indoor=c[key_on])
        rig.fire(on_t + timedelta(minutes=c["dur_min"]), action="idle", indoor=c[key_off])
        prev_sp = sp
    return rig


def test_real_hallway_cycles_learn_the_thermostats_true_heat_hysteresis(tmp_path):
    """31 measured cycles, fine hallway sensor: the thermostat heats ~1.4 F below setpoint."""
    rig = _drive_fixture(tmp_path, key_on="fine_on_f", key_off="fine_off_f", source="sensor")
    assert 1.2 <= rig.swing <= 1.7, rig.model()
    assert rig.count >= 10


def test_real_hallway_cycles_whole_degree_reading_is_an_honest_interval(tmp_path):
    """On the thermostat's own whole-degree reading the start is always setpoint-1 (+/-0.5)."""
    rig = _drive_fixture(tmp_path, key_on="thermostat_on_f", key_off="thermostat_off_f", source="climate")
    assert rig.swing == pytest.approx(1.0)
    assert rig.model()["swing_heat_resolution_f"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 9: migration
# ---------------------------------------------------------------------------


def _write_old_db(path: Path, **cache_overrides) -> None:
    cache = {
        "k_passive": -0.12,
        "k_active_heat": 3.1,
        "observation_count_heat": 12,
        "swing_heat_f": 0.59,
        "swing_cool_f": 0.8,
        "observation_count_swing_heat": 21,
        "observation_count_swing_cool": 4,
    }
    cache.update(cache_overrides)
    path.write_text(json.dumps({"thermal_model_cache": cache, "thermal_observations": []}))


def test_migration_nulls_the_retired_half_rise_values(tmp_path):
    """An existing 0.59 F / 21-count swing is the retired definition: back to the 1.5 default."""
    eng = LearningEngine(tmp_path)
    _write_old_db(eng._db_path)
    eng.load_state()
    model = eng.get_thermal_model()
    assert model["swing_heat_f"] is None
    assert model["swing_cool_f"] is None
    assert model["observation_count_swing_heat"] == 0
    assert model["observation_count_swing_cool"] == 0
    assert model["swing_heat_f_display"] == 1.5
    assert model["swing_cool_f_display"] == 1.5
    assert model["k_active_heat"] == 3.1  # everything else is untouched


def test_migration_runs_once_and_keeps_newly_learned_values(tmp_path):
    """After the reset the stamped cache reloads unchanged (re-learned swing survives restarts)."""
    eng = LearningEngine(tmp_path)
    _write_old_db(eng._db_path)
    eng.load_state()
    eng.record_swing_sample(
        "heat", {"ts": "2026-11-02T10:00:00", "value_f": 1.4, "resolution_f": 0.0, "off_point_f": 0.9}
    )
    eng.save_state()
    eng2 = LearningEngine(tmp_path)
    eng2.load_state()
    assert eng2.get_thermal_model()["swing_heat_f"] == 1.5  # Issue #1477: 1.4 rounded up to the half degree
    assert eng2.get_thermal_model()["swing_heat_raw_f"] == pytest.approx(1.4)
    assert eng2.get_thermal_model()["observation_count_swing_heat"] == 1


# ---------------------------------------------------------------------------
# off-point kept separately; decoupled from the k-fit path
# ---------------------------------------------------------------------------


def test_off_point_is_recorded_separately_and_never_mixed_into_swing(tmp_path, mode):
    rig = _Rig(tmp_path, mode)
    sp = 69.0 if mode == "heat" else 75.0
    _settled(rig, sp)
    rig.cycle(_T0, sp=sp, on_indoor=_past(mode, sp, 1.4), off_indoor=_past(mode, sp, 0.9))
    m = rig.model()
    assert rig.swing == 1.5 and rig.raw == pytest.approx(1.4)  # Issue #1477: stored = raw rounded up to 0.5
    assert m[f"swing_{mode}_off_point_f"] == pytest.approx(0.9)


def test_swing_sample_does_not_depend_on_the_k_fit_observation_committing(tmp_path, mode):
    """The observation pipeline never commits here (it is stubbed), yet the sample lands."""
    rig = _Rig(tmp_path, mode)
    sp = 69.0 if mode == "heat" else 75.0
    _settled(rig, sp)
    rig.cycle(_T0, sp=sp, on_indoor=_past(mode, sp, 1.4), off_indoor=_past(mode, sp, 0.9))
    assert rig.coord.learning.get_thermal_model()["observation_count_heat"] == 0
    assert rig.count == 1


# ---------------------------------------------------------------------------
# 7: replay / production parity through the shared pure function
# ---------------------------------------------------------------------------


def _chart_rows(mode: str, sp: float, on_indoor: float, off_indoor: float, *, prev_sp: float | None = None):
    base = _T0.isoformat()
    prev = {
        "ts": (_T0 - timedelta(minutes=30)).isoformat(),
        "hvac": "idle",
        "indoor": on_indoor + 0.3,
        "setpoint": sp if prev_sp is None else prev_sp,
    }
    on = {
        "ts": base,
        "hvac": _MODES[mode]["running"],
        "indoor": on_indoor,
        "indoor_primary": on_indoor,
        "setpoint": sp,
        "event": "hvac_action_change",
    }
    off = {
        "ts": (_T0 + timedelta(minutes=8)).isoformat(),
        "hvac": "idle",
        "indoor": off_indoor,
        "indoor_primary": off_indoor,
        "setpoint": sp,
        "event": "hvac_action_change",
    }
    return [prev, on, off]


def test_replay_and_production_agree_on_the_same_cycles(tmp_path, mode):
    import importlib

    tr = importlib.import_module("tools.thermal_replay")
    sp = 69.0 if mode == "heat" else 75.0
    cases = [
        (_past(mode, sp, 1.36), _past(mode, sp, -0.18)),  # steady fine
        (_past(mode, sp, 1.0), _past(mode, sp, 1.0)),  # steady whole-degree
        (_past(mode, sp, 1.3), _past(mode, sp, 1.7)),  # wrong direction -> no sample
        (sp, _past(mode, sp, -1.0)),  # on at setpoint -> no sample
    ]
    for i, (on, off) in enumerate(cases):
        (tmp_path / f"c{i}").mkdir()
        rig = _Rig(tmp_path / f"c{i}", mode)
        _settled(rig, sp)
        rig.cycle(_T0, sp=sp, on_indoor=on, off_indoor=off)
        live = rig.swing
        replayed = tr.compute_swing_samples_from_chart_log(_chart_rows(mode, sp, on, off))[mode]
        # Issue #1477: live stores the estimator's output (rounded up to 0.5 F), so compare it with the same
        # estimator over the replayed samples, the path thermal_replay writes (``apply_swing_samples``).
        replay_val = tr._swing.summarize_swing_samples(replayed)["value_f"] if replayed else None
        assert live == replay_val, f"case {i}: live={live} replay={replay_val}"


def test_replay_excludes_a_setpoint_jump_it_can_see(tmp_path):
    import importlib

    tr = importlib.import_module("tools.thermal_replay")
    rows = _chart_rows("heat", 69.0, 67.0, 68.5, prev_sp=65.0)  # setpoint differs from the prior row
    assert tr.compute_swing_samples_from_chart_log(rows)["heat"] == []


# ---------------------------------------------------------------------------
# simulators: asymmetric deadband option (default unchanged)
# ---------------------------------------------------------------------------


def _toy(**over):
    from tools.sim_harness.world import HouseParams, ToyHouse

    params = HouseParams(
        k_env_per_hr=0.1,
        solar_f_hr=0.0,
        heat_gain_f_hr=4.0,
        cool_gain_f_hr=4.0,
        swing_f=over.pop("swing_f", 1.4),
        overshoot_f=0.0,
        whole_degree=False,
        **over,
    )
    return ToyHouse(params, 70.0)


def test_sim_world_asymmetric_heat_deadband_starts_and_stops_where_configured():
    """Start at setpoint-1.4, stop at setpoint-0.9 (the owner's real thermostat)."""
    house = _toy(off_offset_f=-0.9)
    sp = 69.0
    for temp, expected in [(68.0, "idle"), (67.5, "heating"), (68.0, "heating"), (68.5, "idle")]:
        house.indoor_f = temp
        assert house.decide_action("heat", sp) == expected, (temp, expected)


def test_sim_world_asymmetric_cool_deadband_mirrors_heat():
    house = _toy(off_offset_f=-0.9)
    sp = 75.0
    for temp, expected in [(76.0, "idle"), (76.5, "cooling"), (76.0, "cooling"), (75.5, "idle")]:
        house.indoor_f = temp
        assert house.decide_action("cool", sp) == expected, (temp, expected)


def test_sim_world_default_deadband_is_still_symmetric():
    house = _toy(swing_f=1.0)
    for temp, expected in [(69.0, "heating"), (70.0, "heating"), (71.0, "idle")]:
        house.indoor_f = temp
        assert house.decide_action("heat", 70.0) == expected, (temp, expected)


# ---------------------------------------------------------------------------
# Verification follow-ups: reading resolution from the readings (D3), primary reading in chart_log
# (D2), hvac_action flapping
# ---------------------------------------------------------------------------


def test_ecobee_style_tenths_thermostat_reading_is_fine_not_whole_degree(tmp_path):
    """climate_fallback on a thermostat that reports tenths (Ecobee): honest 'fine', not '+/-0.5'."""
    rig = _Rig(tmp_path, "heat", source="climate")
    _settled(rig, 69.0)
    for i, (on, off) in enumerate([(67.6, 68.2), (67.7, 68.3), (67.5, 68.1)]):
        rig.cycle(_T0 + timedelta(hours=i), sp=69.0, on_indoor=on, off_indoor=off)
    assert rig.count == 3
    assert rig.model()["swing_heat_resolution_f"] == 0.0


def test_too_few_distinct_readings_fall_back_to_the_source(tmp_path):
    """One cycle proves nothing about granularity: climate_fallback keeps its whole-degree fallback."""
    rig = _Rig(tmp_path, "heat", source="climate")
    _settled(rig, 69.0)
    rig.cycle(_T0, sp=69.0, on_indoor=67.6, off_indoor=68.2)
    assert rig.model()["swing_heat_resolution_f"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("native", "unit", "expected"),
    [
        ([66.0, 67.0, 68.0, 69.0], "fahrenheit", 1.0),  # whole F
        ([19.0, 20.0, 21.0], "celsius", 1.8),  # whole C = 1.8 F
        ([19.5, 20.0, 20.5, 21.0], "celsius", 0.9),  # half C = 0.9 F
        ([20.3, 20.4, 20.6], "celsius", 0.0),  # tenths C: fine
        ([67.6, 67.7, 68.3], "fahrenheit", 0.0),  # tenths F: fine
        ([68.0, 68.0, 68.0], "fahrenheit", None),  # too few distinct readings
    ],
)
def test_resolution_inferred_from_readings_in_the_reporting_unit(native, unit, expected):
    from custom_components.climate_advisor.swing import infer_resolution_f

    readings_f = [round(c * 1.8 + 32.0, 2) for c in native] if unit == "celsius" else native
    assert infer_resolution_f(readings_f, unit) == expected


def test_summary_resolution_uses_the_samples_reading_unit():
    """Whole-C readings (stored in F) are recognised as 1.8 F granularity via the sample's unit."""
    from custom_components.climate_advisor.swing import evaluate_swing_cycle, summarize_swing_samples

    samples = []
    for i, (sp_c, on_c, off_c) in enumerate([(21, 20, 21), (21, 19, 21), (22, 21, 22)]):
        f = lambda c: c * 1.8 + 32.0  # noqa: E731
        v = evaluate_swing_cycle(
            mode="heat",
            ts=f"2026-11-0{i + 1}T10:00:00",
            setpoint_f=f(sp_c),
            on_indoor_f=f(on_c),
            off_indoor_f=f(off_c),
            off_setpoint_f=f(sp_c),
            resolution_f=0.0,
            reading_unit="celsius",
        )
        samples.append(v.sample)
    assert summarize_swing_samples(samples)["resolution_f"] == pytest.approx(1.8)


def test_hvac_action_flap_does_not_make_a_second_sample(tmp_path, mode):
    """heating -> idle -> heating within seconds is one run, not a new drift to the trigger point."""
    rig = _Rig(tmp_path, mode)
    sp = 69.0 if mode == "heat" else 75.0
    _settled(rig, sp)
    run = _MODES[mode]["running"]
    rig.fire(_T0, action=run, indoor=_past(mode, sp, 1.4))
    rig.fire(_T0 + timedelta(minutes=4), action="idle", indoor=_past(mode, sp, 1.0))
    rig.fire(_T0 + timedelta(minutes=4, seconds=5), action=run, indoor=_past(mode, sp, 1.0))  # flap
    rig.fire(_T0 + timedelta(minutes=9), action="idle", indoor=_past(mode, sp, 0.6))
    assert rig.count == 1
    assert rig.swing == 1.5 and rig.raw == pytest.approx(1.4)  # Issue #1477: stored = raw rounded up to 0.5
    # a genuine later start (minutes after the off) is still a sample
    rig.cycle(_T0 + timedelta(hours=1), sp=sp, on_indoor=_past(mode, sp, 1.4), off_indoor=_past(mode, sp, 0.9))
    assert rig.count == 2


def test_chart_log_hvac_action_row_carries_the_primary_reading_not_the_sleep_swap(tmp_path):
    """During the sleep window ``indoor`` is the bedroom; ``indoor_primary`` must be the hall live swing uses."""
    rig = _Rig(tmp_path, "heat")
    _settled(rig, 69.0)
    rig.coord._get_indoor_temp_with_provenance = lambda: IndoorTempReading(65.5, "sensor.bedroom", 67.6)
    rig.coord._get_indoor_temp = type(rig.coord)._get_indoor_temp.__get__(rig.coord)  # the real comfort read
    rig.coord._chart_log.append.reset_mock()
    rig.fire(_T0, action="heating")
    rows = [
        c.kwargs for c in rig.coord._chart_log.append.call_args_list if c.kwargs.get("event") == "hvac_action_change"
    ]
    assert len(rows) == 1
    assert rows[0]["indoor"] == 65.5 and rows[0]["indoor_primary"] == 67.6


def test_chart_log_indoor_primary_key_only_when_given(tmp_path):
    """The extra key is written only when a value is given, so other rows (and old logs) keep their shape."""
    from custom_components.climate_advisor.chart_log import ChartStateLog

    log = ChartStateLog(tmp_path)
    log.append(hvac="heating", fan=False, indoor=65.5, outdoor=None, event="hvac_action_change", indoor_primary=67.6)
    log.append(hvac="idle", fan=False, indoor=65.5, outdoor=None)
    first, second = log._entries
    assert first["indoor_primary"] == 67.6 and first["indoor"] == 65.5
    assert "indoor_primary" not in second
