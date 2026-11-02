"""Issue #1233 - a homeowner must be able to see WHY the learned swing is what it is.

Occupant framing: the dashboard says "Heat swing 1.4 F below setpoint" (or, on another home, 1.9 F) and
the homeowner wants to check that against their own thermostat. Until #1233 there was no log line, no
stored inputs, and no record of which cycles were thrown away and why - nothing to check the number
against. These tests drive the REAL coordinator thermostat listener (same rig as #1234) and assert
on what is persisted, logged, shown by ``tools/learning_db.py --swing`` and handed to the AI.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.test_swing_hysteresis_1234 import _MODES, _past, _Rig, _settled

_T0 = datetime(2026, 11, 1, 14, 0, 0, tzinfo=UTC)
_TOOLS_DIR = Path(__file__).parent.parent / "tools"
_COORD_LOGGER = "custom_components.climate_advisor.coordinator"


def _tool(name: str):
    spec = importlib.util.spec_from_file_location(name, _TOOLS_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    if str(_TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOLS_DIR))
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(params=["heat", "cool"])
def mode(request) -> str:
    return request.param


def _sp(mode: str) -> float:
    return 69.0 if mode == "heat" else 75.0


def _cache(rig: _Rig) -> dict:
    return rig.coord.learning._state.thermal_model_cache


def _audit(rig: _Rig) -> list[dict]:
    return _cache(rig)[f"swing_{rig.mode}_audit"]


# ---------------------------------------------------------------------------
# 1. Persisted inputs - accepted and every reject reason
# ---------------------------------------------------------------------------

_RECORD_KEYS = {
    "ts",
    "mode",
    "accepted",
    "reason",
    "value_f",
    "setpoint_f",
    "setpoint_age_s",
    "on_indoor_f",
    "off_indoor_f",
    "indoor_entity",
    "resolution_f",
    "reading_unit",
    "trigger",
    "since_prev_off_s",
}


def test_accepted_cycle_persists_every_input(tmp_path, mode):
    rig = _Rig(tmp_path, mode, source="sensor")
    rig.coord.config["indoor_temp_entity"] = "sensor.hall_fine"
    sp = _sp(mode)
    _settled(rig, sp)
    rig.cycle(_T0, sp=sp, on_indoor=_past(mode, sp, 1.36), off_indoor=_past(mode, sp, 0.9))
    (rec,) = _audit(rig)
    assert set(rec) == _RECORD_KEYS
    assert rec["accepted"] is True
    assert rec["reason"] == "ok"
    assert rec["mode"] == mode
    assert rec["value_f"] == pytest.approx(1.36)
    assert rec["setpoint_f"] == pytest.approx(sp)
    assert rec["setpoint_age_s"] == pytest.approx(3 * 3600)  # settled three hours before
    assert rec["on_indoor_f"] == pytest.approx(_past(mode, sp, 1.36))
    assert rec["off_indoor_f"] == pytest.approx(_past(mode, sp, 0.9))
    assert rec["indoor_entity"] == "sensor.hall_fine"
    assert rec["resolution_f"] == 0.0
    assert rec["reading_unit"] == "fahrenheit"
    assert rec["trigger"] == "hvac_action"
    assert rec["since_prev_off_s"] is None
    assert rec["ts"].startswith("2026-11-01T14:00")


def test_climate_fallback_records_the_climate_entity_as_source(tmp_path, mode):
    rig = _Rig(tmp_path, mode, source="climate")
    sp = _sp(mode)
    _settled(rig, sp)
    rig.cycle(_T0, sp=sp, on_indoor=_past(mode, sp, 1.0), off_indoor=_past(mode, sp, 1.0))
    (rec,) = _audit(rig)
    assert rec["indoor_entity"] == "climate.test"
    assert rec["resolution_f"] == pytest.approx(1.0)


def _drive_reject(rig: _Rig, reason: str) -> None:
    mode, sp = rig.mode, _sp(rig.mode)
    _settled(rig, sp)
    run = _MODES[mode]["running"]
    if reason == "setpoint_change":
        rig.fire(_T0, setpoint=sp + 1.0, indoor=sp)
        rig.fire(_T0 + timedelta(seconds=30), action=run, indoor=_past(mode, sp + 1.0, 2.0))
        rig.fire(_T0 + timedelta(minutes=9), action="idle", indoor=_past(mode, sp + 1.0, 0.5))
    elif reason == "short_restart":
        rig.cycle(_T0 - timedelta(hours=1), sp=sp, on_indoor=_past(mode, sp, 1.4), off_indoor=_past(mode, sp, 0.9))
        t = _T0 - timedelta(hours=1) + timedelta(minutes=8, seconds=20)  # 20 s after the off edge
        rig.fire(t, action=run, indoor=_past(mode, sp, 1.2))
        rig.fire(t + timedelta(minutes=5), action="idle", indoor=_past(mode, sp, 0.9))
    elif reason == "wrong_direction":
        rig.cycle(_T0, sp=sp, on_indoor=_past(mode, sp, 1.4), off_indoor=_past(mode, sp, 2.0))
    elif reason == "non_positive":
        rig.cycle(_T0, sp=sp, on_indoor=_past(mode, sp, -0.5), off_indoor=_past(mode, sp, -0.8))
    elif reason == "out_of_bounds":
        rig.cycle(_T0, sp=sp, on_indoor=_past(mode, sp, 6.0), off_indoor=_past(mode, sp, 0.5))
    elif reason == "missing_input":
        rig.fire(_T0, action=run, indoor=_past(mode, sp, 1.4))
        rig._indoor = None  # the sensor went unavailable before the stop edge
        rig.fire(_T0 + timedelta(minutes=8), action="idle")
    else:  # pragma: no cover
        raise AssertionError(reason)


@pytest.mark.parametrize(
    "reason",
    ["setpoint_change", "short_restart", "wrong_direction", "non_positive", "out_of_bounds", "missing_input"],
)
def test_every_reject_reason_is_persisted_with_its_inputs(tmp_path, mode, reason):
    rig = _Rig(tmp_path, mode)
    _drive_reject(rig, reason)
    rec = _audit(rig)[-1]
    assert set(rec) == _RECORD_KEYS
    assert rec["accepted"] is False
    assert rec["reason"] == reason
    assert rec["trigger"] == "hvac_action"
    assert rec["mode"] == mode
    counts = _cache(rig)[f"swing_{mode}_reject_counts"]
    assert counts.get(reason) == 1
    if reason == "out_of_bounds":
        assert rec["value_f"] == pytest.approx(6.0)  # the raw would-be value, so the homeowner sees why
    if reason == "missing_input":
        assert rec["off_indoor_f"] is None and rec["value_f"] == pytest.approx(1.4)  # the start was seen
    if reason == "setpoint_change":
        assert rec["setpoint_age_s"] == pytest.approx(30.0)
    # a rejected cycle never touches the learned value (short_restart has one valid first cycle)
    assert rig.count == (1 if reason == "short_restart" else 0)


def test_audit_is_bounded_and_keeps_the_newest(tmp_path):
    from custom_components.climate_advisor import const

    cap = const.THERMAL_SWING_AUDIT_CAP

    rig = _Rig(tmp_path, "heat")
    sp = 69.0
    _settled(rig, sp)
    for i in range(cap + 7):
        rig.cycle(_T0 + timedelta(minutes=20 * i), sp=sp, on_indoor=sp - 1.3, off_indoor=sp - 0.8)
    audit = _audit(rig)
    assert len(audit) == cap
    assert audit[-1]["ts"].startswith((_T0 + timedelta(minutes=20 * (cap + 6))).isoformat()[:16])


def test_reject_counts_are_cumulative_past_the_audit_window(tmp_path):
    from custom_components.climate_advisor import const

    cap = const.THERMAL_SWING_AUDIT_CAP

    rig = _Rig(tmp_path, "heat")
    sp = 69.0
    _settled(rig, sp)
    n = cap + 3
    for i in range(n):  # every cycle starts at the setpoint -> non_positive
        rig.cycle(_T0 + timedelta(minutes=20 * i), sp=sp, on_indoor=sp, off_indoor=sp + 0.2)
    assert _cache(rig)["swing_heat_reject_counts"]["non_positive"] == n
    assert len(_audit(rig)) == cap


def test_audit_survives_a_save_and_reload(tmp_path):
    from custom_components.climate_advisor.learning import LearningEngine

    rig = _Rig(tmp_path, "heat")
    _settled(rig, 69.0)
    rig.cycle(_T0, sp=69.0, on_indoor=67.6, off_indoor=68.2)
    reloaded = LearningEngine(tmp_path)
    reloaded.load_state()
    cache = reloaded._state.thermal_model_cache
    assert cache["swing_heat_audit"][0]["value_f"] == pytest.approx(1.4)
    assert cache["swing_heat_reject_counts"] == {}


def test_migration_reset_also_clears_the_audit():
    from custom_components.climate_advisor.swing import reset_swing_cache

    cache = {"swing_heat_f": 0.59, "observation_count_swing_heat": 5, "swing_heat_audit": [{"ts": "x"}]}
    assert reset_swing_cache(cache)
    assert "swing_heat_audit" not in cache and "swing_heat_reject_counts" not in cache


# ---------------------------------------------------------------------------
# 2. One log line per sample, zone id on it
# ---------------------------------------------------------------------------


def test_accepted_sample_logs_one_info_line_with_zone_and_median_move(tmp_path, mode, caplog):
    rig = _Rig(tmp_path, mode)
    rig.coord._entry_id = "entry_zone_a"
    sp = _sp(mode)
    _settled(rig, sp)
    with caplog.at_level(logging.DEBUG, logger=_COORD_LOGGER):
        rig.cycle(_T0, sp=sp, on_indoor=_past(mode, sp, 1.36), off_indoor=_past(mode, sp, 0.9))
    lines = [r for r in caplog.records if "Swing sample" in r.getMessage()]
    assert len(lines) == 1
    rec = lines[0]
    msg = rec.getMessage()
    assert rec.levelno == logging.INFO
    assert "accepted" in msg and f"mode={mode}" in msg and "zone=entry_zone_a" in msg
    assert "value=1.36" in msg
    assert "median None->1.36" in msg and "n=1" in msg
    assert f"setpoint={sp:.1f}" in msg


@pytest.mark.parametrize("reason", ["setpoint_change", "wrong_direction", "non_positive", "out_of_bounds"])
def test_rejected_sample_logs_one_debug_line_naming_the_reason(tmp_path, mode, reason, caplog):
    rig = _Rig(tmp_path, mode)
    rig.coord._entry_id = "entry_zone_a"
    with caplog.at_level(logging.DEBUG, logger=_COORD_LOGGER):
        _drive_reject(rig, reason)
    lines = [r for r in caplog.records if "Swing sample" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].levelno == logging.DEBUG
    msg = lines[0].getMessage()
    assert "rejected" in msg and f"reason={reason}" in msg and "zone=entry_zone_a" in msg and f"mode={mode}" in msg


def test_zone_falls_back_to_the_climate_entity_when_there_is_no_entry_id(tmp_path, caplog):
    rig = _Rig(tmp_path, "heat")
    sp = 69.0
    _settled(rig, sp)
    with caplog.at_level(logging.INFO, logger=_COORD_LOGGER):
        rig.cycle(_T0, sp=sp, on_indoor=sp - 1.4, off_indoor=sp - 0.9)
    assert any("zone=climate.test" in r.getMessage() for r in caplog.records if "Swing sample" in r.getMessage())


def test_hvac_observation_start_log_carries_the_zone(caplog):
    import asyncio
    from unittest.mock import patch

    from tests.test_thermal_observation_pipeline import _make_dt_mock, _make_hvac_coord

    coord = _make_hvac_coord(hvac_action="heating")
    coord._entry_id = "entry_zone_a"
    with (
        patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_mock()),
        caplog.at_level(logging.INFO, logger=_COORD_LOGGER),
    ):
        asyncio.run(coord._start_hvac_observation("heat"))
    starts = [r.getMessage() for r in caplog.records if "HVAC observation started" in r.getMessage()]
    assert len(starts) == 1 and "zone=entry_zone_a" in starts[0]
    tries = [r.getMessage() for r in caplog.records if "_start_hvac_observation: " in r.getMessage()]
    assert len(tries) == 1 and "zone=entry_zone_a" in tries[0]


def test_thermal_commit_log_carries_the_zone(tmp_path, caplog):
    from custom_components.climate_advisor.learning import LearningEngine
    from tests.test_thermal_observations import TestAdaptiveVentilatedOLS as _Cls
    from tests.test_thermal_observations import _make_dt_mock as _dt

    helper = _Cls()
    engine = LearningEngine(tmp_path, entry_id="entry_zone_b")
    engine.load_state()
    event = helper._make_event(helper._make_samples(n=20, k_env=-0.10, k_solar=0.0, sf_start=0.0, sf_end=0.0))
    with (
        caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.learning"),
        pytest.MonkeyPatch.context() as mp,
    ):
        mp.setattr("custom_components.climate_advisor.learning.dt_util", _dt(helper._FAKE_DT))
        obs, _, _ = engine._commit_event_from_dict(event, force_grade="high", obs_type="vent_window_decay")
    assert obs is not None
    commits = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Thermal observation committed")]
    assert commits and all("zone=entry_zone_b" in m for m in commits)


def test_every_committed_log_format_in_learning_names_the_zone():
    src = (Path(__file__).parent.parent / "custom_components/climate_advisor/learning.py").read_text()
    import re

    for m in re.finditer(r'"(Thermal observation committed[^"]*|Thermal obs 2-param PRIMARY commit[^"]*)"', src):
        assert "zone=%s" in m.group(1), m.group(1)


# ---------------------------------------------------------------------------
# 3. learning_db --swing formatter
# ---------------------------------------------------------------------------


def _db_with_swing(tmp_path) -> dict:
    rig = _Rig(tmp_path, "heat")
    rig.coord.config["indoor_temp_entity"] = "sensor.hall_fine"
    sp = 69.0
    _settled(rig, sp)
    rig.cycle(_T0, sp=sp, on_indoor=sp - 1.36, off_indoor=sp - 0.9)
    rig.cycle(_T0 + timedelta(hours=1), sp=sp, on_indoor=sp - 1.44, off_indoor=sp - 0.8)
    rig.cycle(_T0 + timedelta(hours=2), sp=sp, on_indoor=sp + 0.2, off_indoor=sp + 0.4)  # non_positive
    rig.fire(_T0 + timedelta(hours=3), setpoint=sp + 1)
    rig.fire(_T0 + timedelta(hours=3, seconds=20), action="heating", indoor=sp - 3)
    rig.fire(_T0 + timedelta(hours=3, minutes=8), action="idle", indoor=sp - 1)  # setpoint_change
    return {"thermal_model_cache": _cache(rig)}


def test_learning_db_swing_report(tmp_path):
    ldb = _tool("learning_db")
    report = ldb.format_swing_debug(_db_with_swing(tmp_path), last_n=10)
    assert "Heat swing" in report and "Cool swing" in report
    assert "how far past the setpoint" in report  # the one-line definition
    assert "recomputed median" in report and "1.40" in report  # median(1.36, 1.44)
    assert "stored 1.40" in report  # stored == recomputed
    assert "sensor.hall_fine" in report
    assert "non_positive" in report and "setpoint_change" in report  # histogram rows
    assert "reject histogram" in report.lower()
    assert "accepted" in report and "rejected" in report


def test_learning_db_swing_last_n_limits_rows(tmp_path):
    ldb = _tool("learning_db")
    db = _db_with_swing(tmp_path)
    one = ldb.format_swing_debug(db, last_n=1)
    all_ = ldb.format_swing_debug(db, last_n=10)
    assert one.count("sensor.hall_fine") < all_.count("sensor.hall_fine")


def test_learning_db_swing_flags_a_stored_value_that_disagrees_with_the_samples(tmp_path):
    ldb = _tool("learning_db")
    db = _db_with_swing(tmp_path)
    db["thermal_model_cache"]["swing_heat_f"] = 0.59
    assert "MISMATCH" in ldb.format_swing_debug(db, last_n=5)


def test_learning_db_swing_handles_a_cache_with_no_swing_data():
    ldb = _tool("learning_db")
    out = ldb.format_swing_debug({"thermal_model_cache": {}}, last_n=5)
    assert "no swing samples" in out.lower()
    assert "no swing samples" in ldb.format_swing_debug({}, last_n=5).lower()


def test_learning_db_swing_on_a_legacy_db_shows_the_stored_value_and_the_retired_warning():
    """A pre-#1234 DB holds a stored swing (e.g. the owner's 0.59) and no samples: say so, don't hide it."""
    ldb = _tool("learning_db")
    out = ldb.format_swing_debug(
        {"thermal_model_cache": {"swing_heat_f": 0.59, "observation_count_swing_heat": 21}}, last_n=5
    )
    assert "no swing samples recorded yet" not in out
    assert "RETIRED definition" in out
    assert "stored 0.59" in out
    assert "no retained samples" in out


def test_learning_db_swing_flag_is_parsed():
    ldb = _tool("learning_db")
    args = ldb._build_parser().parse_args(["--swing", "--last", "7", "--entry-id", "abc"])
    assert args.swing is True and args.last == 7 and args.entry_id == "abc"


# ---------------------------------------------------------------------------
# 4. get_thermal_model / chart data / AI context
# ---------------------------------------------------------------------------


def test_thermal_model_exposes_recent_samples_reject_counts_and_the_definition(tmp_path):
    rig = _Rig(tmp_path, "heat")
    sp = 69.0
    _settled(rig, sp)
    rig.cycle(_T0, sp=sp, on_indoor=sp - 1.4, off_indoor=sp - 0.9)
    rig.cycle(_T0 + timedelta(hours=1), sp=sp, on_indoor=sp + 0.3, off_indoor=sp + 0.5)
    model = rig.model()
    dbg = model["swing_debug"]
    assert "past the setpoint" in dbg["definition"]
    assert [r["accepted"] for r in dbg["heat"]["recent"]] == [True, False]
    assert dbg["heat"]["reject_counts"] == {"non_positive": 1}
    assert dbg["cool"]["recent"] == [] and dbg["cool"]["reject_counts"] == {}
    assert model["swing_definition"] == dbg["definition"]


def test_ai_context_states_the_definition_and_the_reject_counts(tmp_path):
    import asyncio
    from unittest.mock import MagicMock

    from custom_components.climate_advisor.ai_skills_context import build_learning_context  # type: ignore[attr-defined]

    rig = _Rig(tmp_path, "heat")
    sp = 69.0
    _settled(rig, sp)
    rig.cycle(_T0, sp=sp, on_indoor=sp - 1.4, off_indoor=sp - 0.9)
    rig.cycle(_T0 + timedelta(hours=1), sp=sp, on_indoor=sp + 0.3, off_indoor=sp + 0.5)
    coord = MagicMock()
    coord.learning = rig.coord.learning
    coord._build_learning_health = lambda: {}
    coord.config = {"temp_unit": "fahrenheit"}
    text = asyncio.run(build_learning_context(MagicMock(), coord))
    assert "how far past the setpoint" in text
    assert "heat swing: 1.4" in text.lower()
    assert "non_positive x1" in text


def test_chart_payload_helpers_convert_to_the_display_unit():
    from custom_components.climate_advisor.coordinator import _coast_for_display, _swing_debug_for_display

    record = {
        "ts": "2026-11-01T14:00:00+00:00",
        "mode": "heat",
        "accepted": True,
        "reason": "ok",
        "value_f": 1.8,
        "setpoint_f": 68.0,
        "setpoint_age_s": 600.0,
        "on_indoor_f": 66.2,
        "off_indoor_f": 67.1,
        "indoor_entity": "sensor.x",
        "resolution_f": 0.0,
        "reading_unit": "celsius",
        "trigger": "hvac_action",
        "since_prev_off_s": None,
    }
    summary = {"definition": "d", "heat": {"recent": [record], "reject_counts": {"non_positive": 2}}, "cool": {}}
    shown = _swing_debug_for_display(summary, "celsius")["heat"]["recent"][0]
    assert shown["value"] == pytest.approx(1.0)  # a 1.8 F delta is 1.0 C (no +32 offset)
    assert shown["setpoint"] == pytest.approx(20.0)  # an absolute temperature
    assert shown["on_indoor"] == pytest.approx(19.0)
    assert not any(k.endswith("_f") for k in shown)  # no stray degF keys in the display payload
    assert _swing_debug_for_display(summary, "celsius")["heat"]["reject_counts"] == {"non_positive": 2}
    coast = _coast_for_display(
        {"coast_heat_f": 1.8, "coast_heat_past_setpoint_f": -0.9, "observation_count_coast_heat": 4}, "heat", "celsius"
    )
    assert coast == {"value": 1.0, "past_setpoint": -0.5, "count": 4}


def test_chart_thermal_model_payload_carries_swing_debug():
    src = (Path(__file__).parent.parent / "custom_components/climate_advisor/coordinator.py").read_text()
    assert '"swing_debug"' in src and '"coast_heat"' in src and '"coast_cool"' in src


# ---------------------------------------------------------------------------
# 5. Coast overshoot - recorded apart from the swing
# ---------------------------------------------------------------------------


def _coast_cycle(rig: _Rig, on_t: datetime, sp: float, *, k_on: float = 1.4, k_off: float = 0.9):
    """A steady cycle; returns the off time. The off reading is ``k_off`` short of the setpoint."""
    rig.cycle(on_t, sp=sp, on_indoor=_past(rig.mode, sp, k_on), off_indoor=_past(rig.mode, sp, k_off), minutes=8)
    return on_t + timedelta(minutes=8)


def test_heat_peak_and_cool_trough_after_off_are_recorded_when_the_next_run_starts(tmp_path, mode):
    rig = _Rig(tmp_path, mode)
    sp = _sp(mode)
    _settled(rig, sp)
    off_t = _coast_cycle(rig, _T0, sp)
    # the room keeps going the way the equipment was pushing it: 0.9 short -> 0.26 past the setpoint
    for minutes, past in ((1, 0.2), (2, -0.26), (4, -0.1), (6, 0.3)):
        rig.fire(off_t + timedelta(minutes=minutes), indoor=_past(mode, sp, past))
    rig.fire(off_t + timedelta(minutes=12), action=_MODES[mode]["running"], indoor=_past(mode, sp, 1.4))
    m = rig.model()
    assert m[f"coast_{mode}_f"] == pytest.approx(1.16)  # off 0.9 short of the setpoint -> peak 0.26 past it
    assert m[f"observation_count_coast_{mode}"] == 1


def test_coast_value_is_distance_from_the_off_reading(tmp_path, mode):
    rig = _Rig(tmp_path, mode)
    sp = _sp(mode)
    _settled(rig, sp)
    off_t = _coast_cycle(rig, _T0, sp, k_on=1.4, k_off=0.9)
    rig.fire(off_t + timedelta(minutes=2), indoor=_past(mode, sp, -0.26))  # peak/trough
    rig.fire(off_t + timedelta(minutes=5), indoor=_past(mode, sp, 0.1))
    rig.fire(off_t + timedelta(minutes=12), action=_MODES[mode]["running"], indoor=_past(mode, sp, 1.4))
    m = rig.model()
    assert m[f"coast_{mode}_f"] == pytest.approx(1.16)  # off 0.9 short of sp -> 0.26 past it
    assert m[f"coast_{mode}_past_setpoint_f"] == pytest.approx(0.26)
    sample = _cache(rig)[f"coast_{mode}_samples"][0]
    assert sample["ended_by"] == "next_on"
    assert sample["n_post"] == 2
    assert sample["off_indoor_f"] == pytest.approx(_past(mode, sp, 0.9))
    assert sample["extreme_f"] == pytest.approx(_past(mode, sp, -0.26))


def test_coast_never_leaks_into_the_swing(tmp_path, mode):
    rig = _Rig(tmp_path, mode)
    sp = _sp(mode)
    _settled(rig, sp)
    off_t = _coast_cycle(rig, _T0, sp, k_on=1.36, k_off=0.9)
    swing_before = (rig.swing, rig.model()[f"swing_{mode}_off_point_f"], rig.count)
    rig.fire(off_t + timedelta(minutes=2), indoor=_past(mode, sp, -1.5))  # a huge coast
    rig.fire(off_t + timedelta(minutes=12), action=_MODES[mode]["running"], indoor=_past(mode, sp, 1.4))
    assert (rig.swing, rig.model()[f"swing_{mode}_off_point_f"], rig.count) == swing_before
    assert rig.swing == pytest.approx(1.36)
    cache = _cache(rig)
    assert all("coast" not in k for s in cache[f"swing_{mode}_samples"] for k in s)
    assert all("coast" not in k for k in cache[f"swing_{mode}_audit"][0])


def test_setpoint_change_ends_the_coast_without_the_later_readings(tmp_path, mode):
    rig = _Rig(tmp_path, mode)
    sp = _sp(mode)
    _settled(rig, sp)
    off_t = _coast_cycle(rig, _T0, sp)
    rig.fire(off_t + timedelta(minutes=2), indoor=_past(mode, sp, 0.0))
    rig.fire(off_t + timedelta(minutes=4), setpoint=sp + 3, indoor=_past(mode, sp, -0.1))
    rig.fire(off_t + timedelta(minutes=6), indoor=_past(mode, sp, -9.0))  # after the change: not coast
    sample = _cache(rig)[f"coast_{mode}_samples"][0]
    assert sample["ended_by"] == "setpoint_change"
    # the reading that accompanies the change is not coast; the one before it is the extreme
    assert sample["extreme_f"] == pytest.approx(_past(mode, sp, 0.0))
    assert sample["n_post"] == 1
    assert rig.model()[f"observation_count_coast_{mode}"] == 1


def test_hvac_mode_change_ends_the_coast(tmp_path, mode):
    rig = _Rig(tmp_path, mode)
    sp = _sp(mode)
    _settled(rig, sp)
    off_t = _coast_cycle(rig, _T0, sp)
    rig.fire(off_t + timedelta(minutes=2), indoor=_past(mode, sp, 0.0))
    rig.fire(off_t + timedelta(minutes=3), state_mode="off")
    assert _cache(rig)[f"coast_{mode}_samples"][0]["ended_by"] == "setpoint_change"


def test_bounded_time_ends_the_coast_and_ignores_the_late_reading(tmp_path, mode):
    from custom_components.climate_advisor import const

    max_s = const.THERMAL_COAST_MAX_S

    rig = _Rig(tmp_path, mode)
    sp = _sp(mode)
    _settled(rig, sp)
    off_t = _coast_cycle(rig, _T0, sp)
    rig.fire(off_t + timedelta(minutes=10), indoor=_past(mode, sp, -0.3))
    rig.fire(off_t + timedelta(seconds=max_s + 60), indoor=_past(mode, sp, -4.0))
    sample = _cache(rig)[f"coast_{mode}_samples"][0]
    assert sample["ended_by"] == "timeout"
    assert sample["extreme_f"] == pytest.approx(_past(mode, sp, -0.3))


def test_no_post_off_reading_records_no_coast(tmp_path, mode):
    rig = _Rig(tmp_path, mode)
    sp = _sp(mode)
    _settled(rig, sp)
    off_t = _coast_cycle(rig, _T0, sp)
    rig.fire(off_t + timedelta(minutes=9), action=_MODES[mode]["running"], indoor=_past(mode, sp, 1.4))
    assert _cache(rig).get(f"coast_{mode}_samples") in (None, [])
    assert rig.model()[f"coast_{mode}_f"] is None


def test_a_reading_that_moves_the_wrong_way_records_zero_coast_not_negative(tmp_path, mode):
    rig = _Rig(tmp_path, mode)
    sp = _sp(mode)
    _settled(rig, sp)
    off_t = _coast_cycle(rig, _T0, sp, k_off=0.9)
    rig.fire(off_t + timedelta(minutes=3), indoor=_past(mode, sp, 1.2))  # already falling back
    rig.fire(off_t + timedelta(minutes=9), action=_MODES[mode]["running"], indoor=_past(mode, sp, 1.4))
    assert rig.model()[f"coast_{mode}_f"] == 0.0


def test_a_user_turning_the_system_off_in_the_same_event_starts_no_coast(tmp_path, mode):
    rig = _Rig(tmp_path, mode)
    sp = _sp(mode)
    _settled(rig, sp)
    off_t = _coast_cycle(rig, _T0, sp)  # a normal thermostat stop DOES coast: the control case
    rig.fire(off_t + timedelta(minutes=2), indoor=_past(mode, sp, 0.0))
    t = off_t + timedelta(hours=1)
    rig.fire(t, action=_MODES[mode]["running"], indoor=_past(mode, sp, 1.4))
    assert rig.model()[f"observation_count_coast_{mode}"] == 1
    # the occupant switches the system off mid-run: the equipment did not stop by the thermostat's choice
    rig.fire(t + timedelta(minutes=5), action="off", state_mode="off", indoor=_past(mode, sp, 1.0))
    rig.fire(t + timedelta(minutes=8), indoor=_past(mode, sp, 0.0))
    rig.fire(t + timedelta(minutes=12), indoor=_past(mode, sp, 0.0))
    # ... and when the system is switched back on, no coast window is waiting to be closed into a sample
    rig.fire(t + timedelta(minutes=20), state_mode=_MODES[mode]["state"], action=_MODES[mode]["running"])
    assert rig.model()[f"observation_count_coast_{mode}"] == 1


@pytest.mark.parametrize(
    "command",
    [{"setpoint": "drop"}, {"state_mode": "off"}],
    ids=["setpoint_change", "mode_off"],
)
def test_a_commanded_stop_reported_in_a_later_event_starts_no_coast(tmp_path, mode, command):
    """The thermostat often reports the command first and the equipment stopping a few seconds later.

    Occupant outcome: CA's setback (or the occupant switching the system off) must not be recorded as
    "the room keeps going X past the setpoint after the thermostat stops" - that would describe a stop
    nobody's thermostat chose, against a setpoint that was not in force. Mirror of the swing's
    on-edge setpoint-change window (``THERMAL_SWING_SETPOINT_CHANGE_WINDOW_S``).
    """
    rig = _Rig(tmp_path, mode)
    sp = _sp(mode)
    _settled(rig, sp)
    rig.fire(_T0, action=_MODES[mode]["running"], indoor=_past(mode, sp, 1.4))
    t = _T0 + timedelta(minutes=8)
    if "setpoint" in command:
        rig.fire(t, setpoint=sp + _MODES[mode]["past"] * 6.0, indoor=_past(mode, sp, 0.9))  # setback
    else:
        rig.fire(t, state_mode="off", indoor=_past(mode, sp, 0.9))
    rig.fire(t + timedelta(seconds=20), action="idle", indoor=_past(mode, sp, 0.9))  # the stop, one event later
    rig.fire(t + timedelta(minutes=4), indoor=_past(mode, sp, 0.3))
    rig.fire(t + timedelta(minutes=40), indoor=_past(mode, sp, 0.5))  # past the bound: a window would close here
    assert not (rig.coord._coast_candidates or {})
    assert rig.model()[f"observation_count_coast_{mode}"] == 0


def test_coast_median_uses_a_bounded_window(tmp_path):
    from custom_components.climate_advisor import const

    window = const.THERMAL_COAST_ESTIMATOR_WINDOW

    rig = _Rig(tmp_path, "heat")
    sp = 69.0
    _settled(rig, sp)
    for i in range(window + 4):
        t = _T0 + timedelta(minutes=40 * i)
        off_t = _coast_cycle(rig, t, sp)
        rig.fire(off_t + timedelta(minutes=2), indoor=sp - 0.9 + 0.5)
        rig.fire(off_t + timedelta(minutes=12), action="heating", indoor=sp - 1.4)
        rig.fire(off_t + timedelta(minutes=13), action="idle", indoor=sp - 1.0)  # closes this run quickly
    cache = _cache(rig)
    assert len(cache["coast_heat_samples"]) == window


def test_coast_is_persisted_in_the_learning_file(tmp_path):
    from custom_components.climate_advisor.learning import LearningEngine

    rig = _Rig(tmp_path, "heat")
    sp = 69.0
    _settled(rig, sp)
    off_t = _coast_cycle(rig, _T0, sp)
    rig.fire(off_t + timedelta(minutes=2), indoor=sp - 0.9 + 0.8)
    rig.fire(off_t + timedelta(minutes=12), action="heating", indoor=sp - 1.4)
    reloaded = LearningEngine(tmp_path)
    reloaded.load_state()
    assert reloaded.get_thermal_model()["coast_heat_f"] == pytest.approx(0.8)


def test_learning_db_shows_the_coast_apart_from_the_swing(tmp_path):
    ldb = _tool("learning_db")
    rig = _Rig(tmp_path, "heat")
    sp = 69.0
    _settled(rig, sp)
    off_t = _coast_cycle(rig, _T0, sp)
    rig.fire(off_t + timedelta(minutes=2), indoor=sp + 0.26)
    rig.fire(off_t + timedelta(minutes=12), action="heating", indoor=sp - 1.4)
    out = ldb.format_swing_debug({"thermal_model_cache": _cache(rig)}, last_n=5)
    assert "Coast" in out and "next_on" in out
    assert "peak" in out.lower() and "trough" in out.lower()
    assert "not part of the swing" in out.lower()


# ---------------------------------------------------------------------------
# 6. Pure helpers in swing.py
# ---------------------------------------------------------------------------


def test_pure_coast_sample_rules():
    from custom_components.climate_advisor import swing

    cand = {"ts": "2026-11-01T14:08:00+00:00", "off_indoor_f": 68.1, "extreme_f": 69.2, "setpoint_f": 69.0, "n_post": 3}
    v = swing.build_coast_sample("heat", cand, "next_on", 700.0)
    assert v.reason == "ok" and v.sample["coast_f"] == pytest.approx(1.1)
    assert v.sample["past_setpoint_f"] == pytest.approx(0.2)
    assert swing.build_coast_sample("heat", {**cand, "n_post": 0}, "next_on", 60.0).reason == "no_post_readings"
    far = {**cand, "extreme_f": 90.0}
    assert swing.build_coast_sample("heat", far, "timeout", 1800.0).reason == "out_of_bounds"
    assert swing.build_coast_sample("heat", {**cand, "off_indoor_f": None}, "timeout", 1.0).reason == "missing_input"
    assert swing.coast_extreme("heat", 68.0, 68.5) == 68.5 and swing.coast_extreme("cool", 72.0, 71.0) == 71.0
    assert swing.coast_extreme("heat", 68.0, 67.0) == 68.0


def test_reject_reasons_are_the_codes_the_rule_can_return():
    from custom_components.climate_advisor import swing

    assert set(swing.SWING_REJECT_REASONS) == {
        "missing_input",
        "setpoint_change",
        "short_restart",
        "wrong_direction",
        "non_positive",
        "out_of_bounds",
    }


def test_thermal_replay_rebuild_keeps_the_audit_and_the_coast(tmp_path, mode):
    """``thermal_replay --write`` rebuilds the cache from observations; it must not wipe the audit trail.

    Occupant outcome: after a replay the homeowner would otherwise lose every record of why the swing is
    what it is (and the learned coast), with no way to get it back - neither is rebuilt from observations.
    """
    rig = _Rig(tmp_path, mode)
    sp = _sp(mode)
    _settled(rig, sp)
    off_t = _coast_cycle(rig, _T0, sp)
    rig.fire(off_t + timedelta(minutes=3), indoor=_past(mode, sp, 0.2))
    rig.fire(off_t + timedelta(minutes=40), indoor=_past(mode, sp, 0.5))  # past the bound: the window closes
    existing = _cache(rig)
    keys = [
        f"swing_{mode}_audit",
        f"swing_{mode}_reject_counts",
        f"coast_{mode}_samples",
        f"coast_{mode}_f",
        f"coast_{mode}_past_setpoint_f",
        f"observation_count_coast_{mode}",
    ]
    assert all(existing.get(k) not in (None, [], 0) for k in keys if not k.endswith("reject_counts")), existing
    rebuilt = _tool("thermal_replay").rebuild_model_cache([], existing_cache=existing)
    for k in keys:
        assert rebuilt.get(k) == existing.get(k), k


def test_coast_persistence_does_not_delay_the_swing_on_edge(tmp_path):
    """The coast save on an on-edge must not open an await BEFORE the swing cycle is opened.

    HA runs each thermostat state change as its own task, so an await there lets the next event (here:
    the stop) run first and find no open swing cycle - the cycle would silently vanish from the swing
    and its audit. Issue #1233 must not change what the swing learns, even under that interleaving.
    """
    import asyncio
    from unittest.mock import patch

    rig = _Rig(tmp_path, "heat")
    sp = _sp("heat")
    _settled(rig, sp)
    off_t = _coast_cycle(rig, _T0, sp)  # opens a coast window
    rig.fire(off_t + timedelta(minutes=3), indoor=_past("heat", sp, 0.2))
    on_t = off_t + timedelta(minutes=30)

    def ev(old_action, new_action, indoor):
        old = rig._state(old_action, sp, indoor, "heat")
        new = rig._state(new_action, sp, indoor, "heat")
        e = type("E", (), {})()
        e.data = {"old_state": old, "new_state": new}
        return e

    async def slow_job(fn, *args):
        await asyncio.sleep(0.01)  # a real executor hop yields to the loop
        return fn(*args)

    rig.coord._executor_job = slow_job
    rig._indoor = _past("heat", sp, 1.4)

    async def both():
        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.side_effect = [on_t, on_t, on_t + timedelta(minutes=8)] + [on_t + timedelta(minutes=8)] * 50
            t1 = asyncio.create_task(rig.coord._track_swing_edges(*ev("idle", "heating", rig._indoor).data.values()))
            await asyncio.sleep(0)
            rig._indoor = _past("heat", sp, 0.9)
            t2 = asyncio.create_task(rig.coord._track_swing_edges(*ev("heating", "idle", rig._indoor).data.values()))
            await asyncio.gather(t1, t2)

    before = len(_cache(rig)["swing_heat_audit"])
    asyncio.run(both())
    assert len(_cache(rig)["swing_heat_audit"]) == before + 1  # the 8-minute cycle was judged
    assert rig.model()["observation_count_coast_heat"] == 1  # and the coast window still closed at next_on
