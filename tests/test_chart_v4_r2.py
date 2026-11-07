"""Issue #1230 R2: ``get_chart_data()`` carries what the Status chart v2 needs (per-sensor lines, the v4 block).

Occupant view: the Status chart draws a line for each room sensor (hallway, bedroom, the thermostat's finer sensor) so
it never draws one line across the moment Climate Advisor swaps the sensor it reads, and it draws prediction engine
v4's lines only for a house where the published backtest says v4 beat today's prediction. Until then the chart shows
today's prediction alone and says why. All new keys are additive; every existing key keeps its shape and unit.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor import v4_shadow  # noqa: E402
from custom_components.climate_advisor.const import V4_BACKTEST_FILE  # noqa: E402
from custom_components.climate_advisor.storage_paths import resolve_entry_scoped_path  # noqa: E402
from custom_components.climate_advisor.v4_archive import V4Archive, slot_key  # noqa: E402
from tests.test_forecast_chart_integration import (  # noqa: E402
    _NOW,
    _get_coordinator_class,
    _get_coordinator_module,
    _make_full_chart_coord,
    _patched_dt_util,
)

HALL = "climate.hallway"
BED = "sensor.bedroom_temp"
FINE = "sensor.hallway_finer"
ENTRY = "zoneA"

_OLD_KEYS = (
    "predicted_indoor",
    "forecast_outdoor",
    "actual_outdoor",
    "actual_indoor",
    "state_log",
    "target_band",
    "effective_target_history",
    "effective_target_forecast",
    "predicted_activity",
    "unit",
)


def _row(ts: datetime, **kw) -> dict:
    base = {
        "ts": ts.isoformat(),
        "hvac": "off",
        "fan": False,
        "indoor": 70.0,
        "outdoor": 50.0,
        "pred_outdoor": 52.0,
        "pred_indoor": 70.5,
        "windows_open": False,
        "windows_recommended": False,
        "setpoint": None,
        "fan_running": False,
        "nat_vent_active": False,
        "lower": 68.0,
        "upper": 76.0,
        "nat_vent_target": None,
        "indoor_sensors": {HALL: 70.0, BED: 66.0, FINE: 70.5},
    }
    base.update(kw)
    return base


def _rows(n: int = 8) -> list[dict]:
    return [_row(_NOW - timedelta(minutes=30 * k)) for k in range(n, 0, -1)]


def _coord(tmp_path: Path, *, unit="fahrenheit", rows=None, backtest=None, model=None, archive=None, last_future=None):
    cls = _get_coordinator_class()
    coord = _make_full_chart_coord(daily_forecast_full=None, temp_unit=unit)
    coord._chart_log.get_entries = MagicMock(return_value=rows if rows is not None else _rows())
    coord.hass = MagicMock()
    coord.hass.config.config_dir = str(tmp_path)
    coord.hass.states.get = lambda eid: types.SimpleNamespace(
        attributes={"friendly_name": {HALL: "Hallway", BED: "Bedroom"}.get(eid, eid)}
    )
    coord._entry_id = ENTRY
    coord.config["climate_entity"] = HALL
    coord.config["sleep_indoor_temp_entity"] = BED
    coord._indoor_companion_in_use = FINE
    coord._v4_store = types.SimpleNamespace(v4_model=model, v4_mass_f=None)
    coord._v4_archive = archive if archive is not None else V4Archive(tmp_path, entry_id=ENTRY)
    coord._v4_fit_running = False
    coord._v4_last_fit = {}
    coord._v4_last_cycle = {}
    coord._v4_last_future = last_future
    for name in (
        "v4_chart_series",
        "v4_indoor_sensors_meta",
        "v4_model_summary",
        "primary_engine_id",
        "shadow_engine_id",
        "engine_running",
        "prediction_engines_payload",
    ):
        setattr(coord, name, types.MethodType(getattr(cls, name), coord))
    if backtest is not None:
        resolve_entry_scoped_path(tmp_path, V4_BACKTEST_FILE, ENTRY).write_text(json.dumps(backtest), encoding="utf-8")
    return coord


def _chart(coord, range_str="24h", now=_NOW, **kw):
    mod = _get_coordinator_module()
    now_p, as_local_p = _patched_dt_util(mod, now=now)
    with now_p, as_local_p:
        return coord.get_chart_data(range_str, **kw)


def _model() -> dict:
    return {"confidence": "ok", "fitted_at": "2026-11-06T03:15:00", "primary": HALL, "sensors": [HALL, BED, FINE]}


READY = {"gate_summary": {"ready": True, "passed": 9, "scored": 9, "total": 9, "other_sensors_failing": 0}}
NOT_READY = {"gate_summary": {"ready": False, "passed": 4, "scored": 9, "total": 9, "other_sensors_failing": 2}}


# ---------------------------------------------------------------------------
# Per-sensor actual lines
# ---------------------------------------------------------------------------


class TestPerSensorLines:
    def test_one_series_per_sensor_with_names_and_roles(self, tmp_path):
        out = _chart(_coord(tmp_path))
        assert set(out["actual_indoor_sensors"]) == {HALL, BED, FINE}
        by_id = {s["id"]: s for s in out["indoor_sensors"]}
        assert [s["id"] for s in out["indoor_sensors"]] == [HALL, BED, FINE]
        assert by_id[HALL]["name"] == "Hallway" and by_id[HALL]["role"] == "thermostat"
        assert by_id[BED]["role"] == "bedroom sensor"
        assert by_id[FINE]["role"] == "thermostat's finer sensor"
        assert by_id[HALL]["primary"] is True and by_id[BED]["primary"] is False
        pts = out["actual_indoor_sensors"][BED]
        assert len(pts) == 8 and set(pts[0]) == {"time", "temp"} and pts[0]["temp"] == 66.0

    def test_celsius_install_is_converted_once(self, tmp_path):
        out = _chart(_coord(tmp_path, unit="celsius"))
        assert out["unit"] == "celsius"
        # 66 F -> 18.9 C, 70 F -> 21.1 C: never a stray Fahrenheit number on a Celsius axis
        assert all(p["temp"] == pytest.approx(18.9, abs=0.06) for p in out["actual_indoor_sensors"][BED])
        assert max(p["temp"] for s in out["actual_indoor_sensors"].values() for p in s) < 30

    def test_sensor_that_never_read_in_the_window_has_no_line(self, tmp_path):
        rows = [_row(r_ts, indoor_sensors={HALL: 70.0, BED: None}) for r_ts in (_NOW - timedelta(hours=1), _NOW)]
        out = _chart(_coord(tmp_path, rows=rows))
        assert set(out["actual_indoor_sensors"]) == {HALL}
        assert [s["id"] for s in out["indoor_sensors"]] == [HALL]

    def test_gap_is_a_gap_not_a_zero(self, tmp_path):
        rows = _rows(4)
        rows[2]["indoor_sensors"][BED] = None
        out = _chart(_coord(tmp_path, rows=rows))
        assert len(out["actual_indoor_sensors"][BED]) == 3

    def test_rows_without_the_field_give_empty_payload_not_an_error(self, tmp_path):
        rows = [{k: v for k, v in r.items() if k != "indoor_sensors"} for r in _rows(3)]
        out = _chart(_coord(tmp_path, rows=rows))
        assert out["actual_indoor_sensors"] == {} and out["indoor_sensors"] == []

    def test_raw_field_is_still_stripped_from_state_log(self, tmp_path):
        out = _chart(_coord(tmp_path))
        assert all("indoor_sensors" not in e for e in out["state_log"])

    def test_existing_keys_are_untouched(self, tmp_path):
        out = _chart(_coord(tmp_path))
        for key in _OLD_KEYS:
            assert key in out, key
        assert len(out["actual_indoor"]) == 8


# ---------------------------------------------------------------------------
# The v4 block and its gate
# ---------------------------------------------------------------------------


def _archive_with(tmp_path: Path, sensors=(BED, FINE, HALL, "_ca")) -> V4Archive:
    arch = V4Archive(tmp_path, entry_id=ENTRY)
    for k in range(1, 9):
        ts = _NOW - timedelta(minutes=30 * k)
        slot = slot_key(ts)
        for sid in sensors:
            for lead, v in (("1h", 66.1), ("4h", 66.4), ("12h", 66.9), ("24h", 67.0)):
                arch.put(sid, slot, lead, v)  # the archive is columnar since R1b: write through its API
    return arch


def _future() -> dict:
    t0 = _NOW + timedelta(minutes=15)
    pts = [(t0 + timedelta(minutes=15 * k), 66.0 + 0.1 * k) for k in range(8)]
    return {
        "at": _NOW.isoformat(),
        "predictions": {HALL: pts, BED: [(t, v - 4) for t, v in pts], "_ca": pts},
        "corrected": [(t, 53.0) for t, _ in pts],
    }


class TestV4Block:
    def test_no_backtest_does_not_withhold_v4_there_is_no_accuracy_gate(self, tmp_path):
        coord = _coord(tmp_path, model=_model(), archive=_archive_with(tmp_path), last_future=_future())
        v4 = _chart(coord)["v4"]
        assert v4["ready"] is True and v4["reason"] == "ok" and v4["scores"] is None
        # Issue #1400: the "_ca" line is drawn too (broken at every sensor swap), next to the per-sensor lines
        assert set(v4["past"]) == {"_ca", HALL, BED, FINE} and set(v4["future"]) == {"_ca", HALL, BED}
        assert v4["corrected_outdoor"]["future"]

    def test_a_backtest_that_scores_badly_no_longer_withholds_any_line(self, tmp_path):
        coord = _coord(
            tmp_path, model=_model(), archive=_archive_with(tmp_path), last_future=_future(), backtest=NOT_READY
        )
        v4 = _chart(coord)["v4"]
        assert v4["ready"] is True and v4["reason"] == "ok"
        assert v4["scores"] == {"v4_best_rows": 4, "scored_rows": 9, "rows": 9}  # information only
        assert "gate" not in v4 and "note" not in v4
        assert v4["past"] and v4["future"]

    def test_nothing_to_draw_reads_no_model(self, tmp_path):
        coord = _coord(tmp_path)
        v4 = _chart(coord)["v4"]
        assert v4["ready"] is False and v4["reason"] == "no_model" and v4["past"] == {} and v4["future"] == {}

    def test_engines_block_defaults_to_v4_primary_no_shadow(self, tmp_path):
        coord = _coord(
            tmp_path, model={**_model(), "confidence": "medium"}, archive=_archive_with(tmp_path), last_future=_future()
        )
        eng = _chart(coord)["prediction_engines"]
        assert eng["primary"] == "v4" and eng["shadow"] == "none"
        assert [e["id"] for e in eng["engines"]] == ["v3", "v4"]
        v4e = next(e for e in eng["engines"] if e["id"] == "v4")
        assert v4e["running"] and v4e["primary"] and not v4e["shadow"]
        assert eng["primary_state"] == "ready" and eng["fallback"] is False and eng["text"] == ""

    def test_schedule_estimate_is_supplied_only_while_the_primary_is_learning(self, tmp_path, monkeypatch):
        mod = _get_coordinator_module()
        calls = []

        def fake_build(forecast, config, now, **kw):
            calls.append(kw.get("thermal_model"))
            return [{"ts": (now + timedelta(hours=1)).isoformat(), "temp": 69.0}]

        coord = _coord(tmp_path, model=None, archive=_archive_with(tmp_path), last_future=None)
        monkeypatch.setattr(mod, "_build_predicted_indoor_future", fake_build)
        out = _chart(coord)
        # the extra call has NO thermal model: the engine-agnostic schedule path, never another engine
        assert (
            len(calls) == 2
            and calls[-1] == {}
            and out["schedule_estimate"] == [{"ts": out["schedule_estimate"][0]["ts"], "temp": 69.0}]
        )
        ready = _coord(
            tmp_path, model={**_model(), "confidence": "medium"}, archive=_archive_with(tmp_path), last_future=_future()
        )
        calls.clear()
        assert _chart(ready)["schedule_estimate"] == [] and len(calls) == 1

    def test_the_fallback_is_logged_once_per_episode(self, tmp_path, caplog):
        import logging  # noqa: PLC0415

        caplog.set_level(logging.INFO)
        coord = _coord(tmp_path, model=None, archive=_archive_with(tmp_path), last_future=None)
        _chart(coord)
        _chart(coord)
        msgs = [r for r in caplog.records if "no usable fit yet" in r.getMessage()]
        assert len(msgs) == 1
        # Issue #1398: since R6b the primary drives control; without a fit the decisions use defaults, never v3.
        assert "uses v3" not in msgs[0].getMessage()
        assert "default behaviour until it has learned" in msgs[0].getMessage()
        coord._v4_store.v4_model = {**_model(), "confidence": "medium"}
        _chart(coord)  # the episode ends...
        coord._v4_store.v4_model = None
        _chart(coord)  # ...and a new one logs again
        assert len([r for r in caplog.records if "no usable fit yet" in r.getMessage()]) == 2

    def test_v4_not_running_is_not_drawn_not_even_from_the_archive(self, tmp_path):
        coord = _coord(tmp_path, model=_model(), archive=_archive_with(tmp_path), last_future=_future())
        coord.config["primary_engine"] = "v3"  # shadow stays none: v4 is switched off
        out = _chart(coord)
        assert out["v4"]["ready"] is False and out["v4"]["reason"] == "not_running"
        assert out["v4"].get("past", {}) == {} and out["prediction_engines"]["primary"] == "v3"
        v4e = next(e for e in out["prediction_engines"]["engines"] if e["id"] == "v4")
        assert v4e["running"] is False
        coord.config["shadow_engine"] = "v4"  # as the comparison engine it runs again
        assert _chart(coord)["v4"]["ready"] is True

    def test_primary_without_a_usable_fit_falls_back_to_the_schedule_estimate_and_says_so(self, tmp_path):
        coord = _coord(tmp_path, model=None, archive=_archive_with(tmp_path), last_future=None)
        eng = _chart(coord)["prediction_engines"]
        assert eng["fallback"] is True and eng["primary_state"] == "learning"
        assert eng["text"] == "Prediction: learning \u2014 showing the schedule estimate"
        coord._v4_fit_running = True
        eng = _chart(coord)["prediction_engines"]
        assert eng["fitting"] is True and eng["text"] == "Learning: v4 is fitting from your history"

    def test_an_unknown_or_duplicate_selection_reads_as_the_defaults(self, tmp_path):
        coord = _coord(tmp_path, model=_model(), archive=_archive_with(tmp_path), last_future=_future())
        coord.config["primary_engine"] = "v9"
        coord.config["shadow_engine"] = "v9"
        eng = _chart(coord)["prediction_engines"]
        assert eng["primary"] == "v4" and eng["shadow"] == "none"
        coord.config["primary_engine"] = "v3"
        coord.config["shadow_engine"] = "v3"  # equals the primary: no comparison engine
        assert _chart(coord)["prediction_engines"]["shadow"] == "none"

    def test_ready_zone_gets_past_lines_per_sensor_and_lead_and_future_lines(self, tmp_path):
        coord = _coord(tmp_path, model=_model(), archive=_archive_with(tmp_path), last_future=_future(), backtest=READY)
        v4 = _chart(coord)["v4"]
        assert v4["ready"] is True and v4["reason"] == "ok" and v4["leads"] == ["1h", "4h", "12h", "24h"]
        assert set(v4["past"]) == {"_ca", HALL, BED, FINE}  # "_ca" is drawn, broken at the swaps (Issue #1400)
        assert set(v4["past"][BED]) == {"1h", "4h", "12h", "24h"}
        assert len(v4["past"][BED]["4h"]) == 8 and v4["past"][BED]["4h"][0]["temp"] == 66.4
        assert set(v4["future"]) == {"_ca", HALL, BED}  # FINE has no future prediction this cycle
        assert v4["future"][HALL][0]["temp"] == 66.0 and len(v4["future"][HALL]) == 8
        assert v4["future_at"] == _NOW.isoformat()
        assert v4["model"]["confidence"] == "ok"
        assert [p["temp"] for p in v4["corrected_outdoor"]["future"]] == [53.0] * 8

    def test_celsius_for_every_v4_line(self, tmp_path):
        coord = _coord(
            tmp_path,
            unit="celsius",
            model=_model(),
            archive=_archive_with(tmp_path),
            last_future=_future(),
            backtest=READY,
        )
        v4 = _chart(coord)["v4"]
        assert v4["past"][BED]["4h"][0]["temp"] == pytest.approx(19.1, abs=0.06)  # 66.4 F
        assert v4["future"][HALL][0]["temp"] == pytest.approx(18.9, abs=0.06)  # 66.0 F
        assert v4["corrected_outdoor"]["future"][0]["temp"] == pytest.approx(11.7, abs=0.06)  # 53 F
        everything = [p["temp"] for s in v4["past"].values() for pts in s.values() for p in pts]
        everything += [p["temp"] for pts in v4["future"].values() for p in pts]
        assert max(everything) < 25

    def test_historical_view_has_no_future_lines(self, tmp_path):
        coord = _coord(tmp_path, model=_model(), archive=_archive_with(tmp_path), last_future=_future(), backtest=READY)
        aware_now = _NOW.replace(tzinfo=UTC)
        v4 = _chart(coord, now=aware_now, before_ts=(aware_now - timedelta(days=2)).timestamp())["v4"]
        assert v4["future"] == {} and v4["corrected_outdoor"]["future"] == []

    def test_past_lines_fall_back_to_the_backtest_for_older_slots(self, tmp_path):
        old = _NOW - timedelta(hours=20)
        bt = dict(READY)
        bt["points"] = {BED: {"4h": [{"t": old.isoformat(), "actual": 66.0, "flat": 66.0, "v3": 66.0, "v4": 65.0}]}}
        coord = _coord(tmp_path, model=_model(), archive=_archive_with(tmp_path), last_future=_future(), backtest=bt)
        pts = _chart(coord)["v4"]["past"][BED]["4h"]
        assert len(pts) == 9 and pts[0]["temp"] == 65.0  # the older backtest slot, then the 8 live ones

    def test_long_ranges_are_downsampled_like_the_chart_log(self, tmp_path):
        arch = V4Archive(tmp_path, entry_id=ENTRY)
        for k in range(1, 48 * 6):
            arch.put(BED, slot_key(_NOW - timedelta(minutes=30 * k)), "4h", 66.0)
        coord = _coord(tmp_path, model=_model(), archive=arch, last_future=None, backtest=READY)
        raw = _chart(coord, "3d")["v4"]["past"][BED]["4h"]
        hourly = _chart(coord, "7d")["v4"]["past"][BED]["4h"]
        daily = _chart(coord, "1y")["v4"]["past"][BED]["4h"]
        assert len(raw) > 130 and len(hourly) < 145 and len(daily) <= 7

    def test_corrected_outdoor_history_uses_the_model_bias(self, tmp_path, monkeypatch):
        calls = {}

        class _Engine:
            def from_dict(self, d):
                return d

            def corrected_forecast(self, model, *, times, raw_f, tz, now=None, **_kw):
                calls["now"] = now
                return [r + 1.5 for r in raw_f]

        monkeypatch.setattr(v4_shadow, "_predictor", lambda: _Engine())
        mod = _get_coordinator_module()
        monkeypatch.setattr(mod.dt_util, "parse_datetime", datetime.fromisoformat)
        coord = _coord(tmp_path, model=_model(), archive=_archive_with(tmp_path), backtest=READY)
        past = _chart(coord)["v4"]["corrected_outdoor"]["past"]
        assert len(past) == 8 and past[0]["temp"] == 53.5 and calls["now"] is None  # pred_outdoor 52 + bias 1.5

    def test_a_broken_v4_never_breaks_chart_data(self, tmp_path):
        coord = _coord(tmp_path, model=_model(), archive=_archive_with(tmp_path), backtest=READY)
        coord._v4_archive = None
        out = _chart(coord)
        assert out["v4"].get("past", {}) == {}
        for key in _OLD_KEYS:
            assert key in out

    def test_zone_without_the_shadow_reports_it_off(self, tmp_path):
        coord = _coord(tmp_path)
        del coord._v4_store
        out = _chart(coord)
        assert out["v4"]["reason"] == "shadow_off" and out["v4"]["ready"] is False
