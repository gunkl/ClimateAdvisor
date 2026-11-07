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
    for name in ("v4_chart_series", "v4_indoor_sensors_meta", "v4_model_summary"):
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
            arch.data.setdefault(sid, {})[slot] = {"1h": 66.1, "4h": 66.4, "12h": 66.9, "24h": 67.0}
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
    def test_no_backtest_means_v4_is_not_drawn_and_says_why(self, tmp_path):
        coord = _coord(tmp_path, model=_model(), archive=_archive_with(tmp_path), last_future=_future())
        v4 = _chart(coord)["v4"]
        assert v4["ready"] is False and v4["reason"] == "no_backtest"
        assert v4["past"] == {} and v4["future"] == {} and v4["corrected_outdoor"] == {"past": [], "future": []}

    def test_failing_gate_withholds_every_line(self, tmp_path):
        coord = _coord(
            tmp_path, model=_model(), archive=_archive_with(tmp_path), last_future=_future(), backtest=NOT_READY
        )
        v4 = _chart(coord)["v4"]
        assert v4["ready"] is False and v4["reason"] == "not_ready"
        assert v4["gate"]["passed"] == 4 and v4["gate"]["total"] == 9
        assert v4["past"] == {} and v4["future"] == {}

    @pytest.mark.parametrize("flag", ["true", 1, "yes", None])
    def test_only_a_literal_true_opens_the_gate(self, tmp_path, flag):
        bt = {"gate_summary": {"ready": flag}}
        coord = _coord(tmp_path, model=_model(), archive=_archive_with(tmp_path), last_future=_future(), backtest=bt)
        assert _chart(coord)["v4"]["ready"] is False

    def test_ready_zone_gets_past_lines_per_sensor_and_lead_and_future_lines(self, tmp_path):
        coord = _coord(tmp_path, model=_model(), archive=_archive_with(tmp_path), last_future=_future(), backtest=READY)
        v4 = _chart(coord)["v4"]
        assert v4["ready"] is True and v4["reason"] == "ok" and v4["leads"] == ["1h", "4h", "12h", "24h"]
        assert set(v4["past"]) == {HALL, BED, FINE}  # "_ca" follows the sensor swap: never drawn
        assert set(v4["past"][BED]) == {"1h", "4h", "12h", "24h"}
        assert len(v4["past"][BED]["4h"]) == 8 and v4["past"][BED]["4h"][0]["temp"] == 66.4
        assert set(v4["future"]) == {HALL, BED}  # FINE has no future prediction this cycle
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
            arch.data.setdefault(BED, {})[slot_key(_NOW - timedelta(minutes=30 * k))] = {"4h": 66.0}
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
        assert out["v4"]["ready"] is False
        for key in _OLD_KEYS:
            assert key in out

    def test_zone_without_the_shadow_reports_it_off(self, tmp_path):
        coord = _coord(tmp_path)
        del coord._v4_store
        out = _chart(coord)
        assert out["v4"]["reason"] == "shadow_off" and out["v4"]["ready"] is False
