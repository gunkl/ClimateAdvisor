"""Issue #1233 - ``tools/swing_audit.py``: check the learned swing against the real equipment.

Occupant framing: a homeowner (the owner's hallway, or the user who reports about 1.9 F) wants to know
whether Climate Advisor's "Heat swing 1.4 F below setpoint" matches what their own thermostat really
does. The audit measures the real firing offsets from recorder history and judges every cycle with the
SAME rule production uses (``swing.evaluate_swing_cycle``), so the two cannot disagree.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).parent.parent / "tools"
_FIXTURE = Path(__file__).parent / "fixtures" / "swing_hallway_cycles_1234.json"


def _load():
    spec = importlib.util.spec_from_file_location("swing_audit", _TOOLS_DIR / "swing_audit.py")
    mod = importlib.util.module_from_spec(spec)
    if str(_TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOLS_DIR))
    spec.loader.exec_module(mod)
    return mod


def _fixture_cycles() -> list[dict]:
    """The 31 real hallway cycles, as audit cycles measured on the fine 0.18 F sensor."""
    raw = json.loads(_FIXTURE.read_text())["cycles"]
    out: list[dict] = []
    prev_off: datetime | None = None
    for c in raw:
        on = datetime.fromisoformat(c["on_utc"]).replace(tzinfo=UTC)
        off = on + timedelta(minutes=c["dur_min"])
        out.append(
            {
                "on_ts": on.isoformat(),
                "off_ts": off.isoformat(),
                "setpoint_f": c["setpoint_f"],
                "off_setpoint_f": c["setpoint_f"],
                "setpoint_age_s": c["setpoint_age_min"] * 60.0,
                "on_indoor_f": c["fine_on_f"],
                "off_indoor_f": c["fine_off_f"],
                "since_prev_off_s": (on - prev_off).total_seconds() if prev_off else None,
                "mode": "heat",
            }
        )
        prev_off = off
    return out


def test_real_hallway_cycles_measure_the_thermostats_firing_offset():
    """The owner's measured 1.36-1.46 F (steady cycles) comes out of the audit."""
    audit = _load().audit_cycles(_fixture_cycles())
    heat = audit["heat"]
    assert 1.36 <= heat["summary"]["raw_value_f"] <= 1.46, heat["summary"]
    assert heat["summary"]["value_f"] == 1.5  # Issue #1477: stored rounded up to the next half degree
    assert heat["summary"]["n"] >= 10
    # CA's own morning raises (setpoint age 0) are not hysteresis and must be excluded
    assert heat["reject_counts"].get("setpoint_change", 0) >= 5
    assert heat["reject_counts"].get("wrong_direction", 0) >= 1  # the 0.4-minute fall at 28 Oct 13:34
    assert len(heat["rows"]) == 31
    assert {r["reason"] for r in heat["rows"] if r["accepted"]} == {"ok"}


def test_audit_reuses_the_production_rule_not_a_copy():
    mod = _load()
    prod = Path(__file__).parent.parent / "custom_components/climate_advisor/swing.py"
    assert Path(mod._swing.__file__).resolve() == prod.resolve()
    src = (_TOOLS_DIR / "swing_audit.py").read_text()
    assert "def evaluate_swing_cycle" not in src
    assert "THERMAL_SWING_SETPOINT_CHANGE_WINDOW_S =" not in src
    assert "evaluate_swing_cycle(" in src


# --------------------------------------------------------------------------
# One history, two readers: the live coordinator and the audit must agree
# --------------------------------------------------------------------------


def _one_history(mode: str, seed: int, sensor: bool):
    """Drive ONE seeded thermostat history through the real coordinator listener (the #1234 rig) and
    write the same history as ``ha_logs.py --history`` text. Returns (rig, climate_text, sensor_text).

    The stream mixes real starts/stops, setpoint and HVAC-mode changes (including a stop reported a few
    seconds after the command), fan, flaps and plain temperature updates. With ``sensor`` the indoor
    reading is a dedicated sensor that ticks between thermostat events (fed to the live sensor hook)."""
    import random
    from unittest.mock import patch

    from tests.test_swing_hysteresis_1234 import _Rig

    r = random.Random(seed)
    rig = _Rig(_TMP_FACTORY(), mode, source="sensor" if sensor else "climate_fallback")
    t = datetime(2026, 10, 1, tzinfo=UTC)
    clim = ["=== History for climate.x ===", _row(t - timedelta(hours=1), rig)]
    sens = ["=== History for sensor.x ==="]
    indoor = 70.0
    last_tick = t - timedelta(hours=1)
    sens.append(f"{last_tick.isoformat()} | {rig._indoor} | x=1")

    def tick(at, value):
        nonlocal last_tick
        rig._indoor = value
        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = at
            rig.coord._coast_feed(at)
        sens.append(f"{at.isoformat()} | {value} | x=1")
        last_tick = at

    for _ in range(260):
        t += timedelta(seconds=r.choice([5, 20, 45, 90, 130, 300, 600, 900]))
        indoor = round(indoor + r.uniform(-0.9, 0.9), 2)
        if sensor and (r.random() < 0.45 or (t - last_tick).total_seconds() > 1500):
            tick(t, indoor)
            continue
        k = r.random()
        kw: dict = {}
        if k < 0.12:
            kw["setpoint"] = r.choice([66.0, 68.0, 70.0, 72.0, 75.0])
        elif k < 0.17:
            kw["state_mode"] = r.choice([mode, "off"])
        elif k < 0.6:
            kw["action"] = r.choice([rig.spec["running"], "idle", "fan", rig.spec["running"]])
        if sensor:
            rig.indoor_attr = float(round(indoor))  # the thermostat's own whole-degree attribute
            rig.fire(t, **kw)
        else:
            rig.fire(t, indoor=indoor, **kw)
        clim.append(_row(t, rig))
    return rig, "\n".join(clim), "\n".join(sens)


def _row(at: datetime, rig) -> str:
    return (
        f"{at.isoformat()} | {rig.state_mode} | current_temperature={rig.indoor_attr}, temperature={rig.setpoint}, "
        f"target_temp_high=None, target_temp_low=None, hvac_action={rig.action}"
    )


_TMP_FACTORY = None


@pytest.fixture
def _tmp_factory(tmp_path_factory):
    global _TMP_FACTORY
    _TMP_FACTORY = lambda: tmp_path_factory.mktemp("rig")  # noqa: E731
    yield
    _TMP_FACTORY = None


def _key(ts) -> str:
    return datetime.fromisoformat(str(ts)).isoformat()


@pytest.mark.parametrize("sensor", [False, True], ids=["climate_fallback", "fine_sensor"])
@pytest.mark.parametrize("seed", range(4))
@pytest.mark.parametrize("mode", ["heat", "cool"])
def test_the_audit_reads_a_history_exactly_as_the_live_coordinator_did(_tmp_factory, mode, seed, sensor):
    """Occupant framing: the audit is how a homeowner checks the learned swing and coast. It is only
    trustworthy if, fed the history of what the thermostat did, it reaches the SAME verdict, value and
    inputs for every cycle - and the same coast windows - that the integration recorded live."""
    mod = _load()
    rig, clim_text, sens_text = _one_history(mode, seed, sensor)
    audit = mod.audit_history(
        mod.parse_climate_history(clim_text),
        sensor_rows=mod.parse_sensor_history(sens_text) if sensor else None,
        unit="fahrenheit",
    )
    cache = rig.coord.learning._state.thermal_model_cache or {}
    fields = ("reason", "value_f", "setpoint_f", "setpoint_age_s", "on_indoor_f", "off_indoor_f", "resolution_f")
    live = [{"ts": _key(x["ts"]), **{f: x[f] for f in fields}} for x in cache.get(f"swing_{mode}_audit") or []]
    rows = audit[mode]["rows"][-len(live) :] if live else []
    replay = [{"ts": _key(x["ts"]), **{f: x[f] for f in fields}} for x in rows]
    assert live, "the stream must produce judged cycles"
    assert replay == live
    assert len(audit[mode]["rows"]) == len(cache.get(f"swing_{mode}_audit") or []) or len(live) == 50
    # coast: same windows, same values, same reason they ended
    live_coast = cache.get(f"coast_{mode}_samples") or []
    replay_coast = audit[mode]["coast_samples"][-len(live_coast) :] if live_coast else []
    assert [{**c, "ts": _key(c["ts"])} for c in replay_coast] == [{**c, "ts": _key(c["ts"])} for c in live_coast]
    assert len(audit[mode]["coast_samples"]) == cache.get(f"observation_count_coast_{mode}", 0)


def test_the_comparison_streams_exercise_every_coast_ending_and_several_reject_reasons(_tmp_factory):
    """Guard against a vacuous comparison: across the streams, coast windows end all three ways and the
    swing sees accepted and rejected cycles."""
    mod = _load()
    endings: set[str] = set()
    reasons: set[str] = set()
    for mode in ("heat", "cool"):
        for seed in range(4):
            for sensor in (False, True):
                _rig, clim_text, sens_text = _one_history(mode, seed, sensor)
                audit = mod.audit_history(
                    mod.parse_climate_history(clim_text),
                    sensor_rows=mod.parse_sensor_history(sens_text) if sensor else None,
                )
                endings |= {c["ended_by"] for c in audit[mode]["coast_samples"]}
                reasons |= {r["reason"] for r in audit[mode]["rows"]}
    assert endings == {"next_on", "setpoint_change", "timeout"}
    assert {"ok", "setpoint_change", "wrong_direction"} <= reasons, reasons


# --------------------------------------------------------------------------
# Parsing ha_logs --history output
# --------------------------------------------------------------------------

_ATTR = "current_temperature={ct}, temperature={sp}, target_temp_high=None, target_temp_low=None, hvac_action={act}"


def _climate_line(ts: str, mode: str, ct: float, sp: float, act: str) -> str:
    return f"{ts} | {mode} | " + _ATTR.format(ct=ct, sp=sp, act=act)


def _history_text() -> tuple[str, str]:
    """Three steady heat cycles (+ one setpoint jump) as ha_logs --history prints them."""
    clim = ["=== History for climate.hall (last 72h) ==="]
    sens = ["=== History for sensor.hall_temp (last 72h) ==="]
    base = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
    clim.append(_climate_line(base.isoformat(), "heat", 69, 69, "idle"))
    for i in range(3):
        on = base + timedelta(hours=1 + i)
        off = on + timedelta(minutes=8)
        sens.append(f"{(on - timedelta(seconds=40)).isoformat()} | 67.64 | state_class=measurement")
        clim.append(_climate_line(on.isoformat(), "heat", 68, 69, "heating"))
        sens.append(f"{(off - timedelta(seconds=5)).isoformat()} | 68.18 | state_class=measurement")
        clim.append(_climate_line(off.isoformat(), "heat", 68, 69, "idle"))
        sens.append(f"{(off + timedelta(minutes=3)).isoformat()} | 69.26 | state_class=measurement")
        sens.append(f"{(off + timedelta(minutes=9)).isoformat()} | 68.9 | state_class=measurement")
    # a setpoint raise followed 30 s later by a start: a jump, not hysteresis
    jump = base + timedelta(hours=6)
    clim.append(_climate_line(jump.isoformat(), "heat", 68, 71, "idle"))
    sens.append(f"{(jump + timedelta(seconds=10)).isoformat()} | 67.64 | state_class=measurement")
    clim.append(_climate_line((jump + timedelta(seconds=30)).isoformat(), "heat", 68, 71, "heating"))
    clim.append(_climate_line((jump + timedelta(minutes=9)).isoformat(), "heat", 70, 71, "idle"))
    sens.append(f"{(jump + timedelta(minutes=9)).isoformat()} | 70.5 | state_class=measurement")
    return "\n".join(clim), "\n".join(sens)


def test_audit_from_ha_logs_history_text_uses_the_fine_sensor():
    mod = _load()
    clim_text, sens_text = _history_text()
    audit = mod.audit_history(
        mod.parse_climate_history(clim_text), sensor_rows=mod.parse_sensor_history(sens_text), unit="fahrenheit"
    )
    heat = audit["heat"]
    assert heat["summary"]["n"] == 3
    assert heat["summary"]["raw_value_f"] == pytest.approx(1.36)  # Issue #1477: value_f is that rounded up (1.5)
    assert heat["reject_counts"] == {"setpoint_change": 1}
    # the peak the room reached after heat-off is reported next to (never inside) the swing
    assert heat["coast"]["median_f"] == pytest.approx(69.26 - 68.18)
    assert heat["summary"]["off_point_f"] == pytest.approx(0.82)


def test_audit_without_a_fine_sensor_falls_back_to_the_thermostat_whole_degrees():
    mod = _load()
    clim_text, _ = _history_text()
    audit = mod.audit_history(mod.parse_climate_history(clim_text), sensor_rows=None, unit="fahrenheit")
    assert audit["heat"]["summary"]["value_f"] == pytest.approx(1.0)
    assert audit["heat"]["summary"]["resolution_f"] in (0.0, 1.0)
    assert "thermostat current_temperature" in mod.format_report(audit, learned={})


def test_audit_report_compares_the_learned_value():
    mod = _load()
    clim_text, sens_text = _history_text()
    audit = mod.audit_history(
        mod.parse_climate_history(clim_text), sensor_rows=mod.parse_sensor_history(sens_text), unit="fahrenheit"
    )
    report = mod.format_report(audit, learned={"heat": 0.59, "cool": None})
    assert "measured 1.36" in report and "learned 0.59" in report
    assert "DIFFERS" in report
    ok = mod.format_report(audit, learned={"heat": 1.40, "cool": None})
    assert "matches" in ok and "DIFFERS" not in ok


def test_celsius_history_is_converted_to_fahrenheit_before_judging():
    mod = _load()
    # 20.56 C setpoint = 69.0 F; sensor 19.80 C = 67.64 F -> swing 1.36 F
    rows = mod.parse_climate_history(
        "\n".join(
            [
                _climate_line("2026-11-01T06:00:00+00:00", "heat", 21, 20.56, "idle"),
                _climate_line("2026-11-01T07:00:00+00:00", "heat", 20, 20.56, "heating"),
                _climate_line("2026-11-01T07:08:00+00:00", "heat", 20, 20.56, "idle"),
            ]
        ),
        unit="celsius",
    )
    sens = mod.parse_sensor_history(
        "2026-11-01T06:59:30+00:00 | 19.80 | x=1\n2026-11-01T07:07:55+00:00 | 20.10 | x=1", unit="celsius"
    )
    audit = mod.audit_history(rows, sensor_rows=sens, unit="celsius")
    assert audit["heat"]["summary"]["raw_value_f"] == pytest.approx(1.36, abs=0.02)  # Issue #1477: raw median


def test_cooling_cycles_are_audited_too():
    mod = _load()
    clim = "\n".join(
        [
            _climate_line("2026-07-01T06:00:00+00:00", "cool", 75, 75, "idle"),
            _climate_line("2026-07-01T09:00:00+00:00", "cool", 76, 75, "cooling"),
            _climate_line("2026-07-01T09:10:00+00:00", "cool", 75, 75, "idle"),
        ]
    )
    sens = "2026-07-01T08:59:50+00:00 | 76.4 | x=1\n2026-07-01T09:09:55+00:00 | 75.2 | x=1"
    audit = mod.audit_history(mod.parse_climate_history(clim), sensor_rows=mod.parse_sensor_history(sens))
    assert audit["cool"]["summary"]["raw_value_f"] == pytest.approx(1.4)  # Issue #1477: stored as 1.5
    assert audit["heat"]["rows"] == []


def test_cli_offline_mode_reads_saved_history_files(tmp_path, capsys):
    mod = _load()
    clim_text, sens_text = _history_text()
    (tmp_path / "climate.hall.txt").write_text(clim_text)
    (tmp_path / "sensor.hall_temp.txt").write_text(sens_text)
    rc = mod.main(
        ["--climate-file", str(tmp_path / "climate.hall.txt"), "--sensor-file", str(tmp_path / "sensor.hall_temp.txt")]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "1.36" in out and "Heat" in out


def test_cli_prints_no_secrets(tmp_path, capsys, monkeypatch):
    mod = _load()
    monkeypatch.setenv("HA_API_TOKEN", "SECRET-TOKEN-VALUE")
    clim_text, sens_text = _history_text()
    (tmp_path / "c.txt").write_text(clim_text)
    mod.main(["--climate-file", str(tmp_path / "c.txt")])
    captured = capsys.readouterr()
    assert "SECRET-TOKEN-VALUE" not in captured.out + captured.err
    src = (_TOOLS_DIR / "swing_audit.py").read_text()
    assert "/run/secrets" not in src and ".deploy.env" not in src.replace("load_config", "")
