"""Issue #1406: the chart history draws a past time-of-use bank as what the thermostat held.

Occupant view: scrolling the chart back over an Away cool bank (cool to 77 F inside the 60-80 F Away band) drew the
band in its schedule shape, 77-80 F, while the live chart (since #1404) drew the hold, 60-77 F, so history and live
disagreed about the same afternoon. chart_log now records the band the chart DREW on each row, ``hold_lower`` /
``hold_upper``, only when it differs from ``lower`` / ``upper`` (which keep their meaning for the replay tools,
the prediction and the comfort score); history draws the hold when a row has it and the old shape otherwise.

These tests drive the real ``ChartStateLog`` (append, hourly and daily buckets), the real coordinator
``_chart_display_band()`` the live chart uses (``_read_chart_hold``), and the real ``get_chart_data()`` historical
path. Nothing re-implements the band.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.const import OCCUPANCY_HOME  # noqa: E402
from tests.test_current_hold_1404 import NOW, OPEN, PEAK, _chart_coord, _coordinator  # noqa: E402
from tests.test_forecast_chart_integration import (  # noqa: E402
    _NOW,
    _daily_6day_forecast,
    _make_full_chart_coord,
    _patched_dt_util,
)


def _coord_mod():
    return importlib.import_module("custom_components.climate_advisor.coordinator")


def _chart_log_mod():
    return importlib.import_module("custom_components.climate_advisor.chart_log")


# ── the coordinator: the hold comes from the live chart's own band ─────────────────────────────────────────────────
def _hold_at(coord, at: datetime) -> tuple[tuple, dict]:
    """(lower, upper) the row persists and the hold fields ``_read_chart_hold`` adds, at ``at``."""
    mod = _coord_mod()
    coord._target_band_schedule = coord._build_target_band_for(at, None)
    with (
        patch.object(mod.dt_util, "now", return_value=at),
        patch.object(mod.dt_util, "as_local", side_effect=lambda x: x),
    ):
        return coord._target_band_lower_upper_now(), coord._read_chart_hold()


class TestRowHold:
    def test_away_cool_bank_row_records_the_hold(self) -> None:
        """Bank owns the thermostat: lower/upper keep the schedule's 77-80, the hold is what the chart draws, 60-77."""
        band, hold = _hold_at(_chart_coord(), NOW)
        assert band == (77.0, 80.0)
        assert hold == {"hold_lower": 60.0, "hold_upper": 77.0}

    def test_hold_is_the_live_chart_band_for_the_same_instant(self) -> None:
        coord = _chart_coord()
        _band, hold = _hold_at(coord, NOW)
        live = {e["ts"]: e for e in coord._chart_display_band(NOW, None, raw_band=coord._target_band_schedule)}
        drawn = live[NOW.isoformat()]
        assert (hold["hold_lower"], hold["hold_upper"]) == (drawn["lower"], drawn["upper"])

    def test_away_heat_bank_row_records_the_hold(self) -> None:
        """Mirror: an Away pre-heat bank to 63 F. The schedule keeps the #786 shape (63 on the UPPER edge: 60-63);
        the thermostat holds heat 63 inside the 60-80 band, so the chart draws 63-80."""
        coord = _coordinator(mode="heat", target=63.0)

        def build(self, now, thermal_model, hourly_forecast_override=None, *, with_tou_window=True):
            out = []
            for h in range(11, 18):
                ts = datetime(2026, 7, 13, h, 0, tzinfo=UTC)
                upper = 63.0 if with_tou_window and OPEN <= ts < PEAK else 80.0
                out.append({"ts": ts.isoformat(), "lower": 60.0, "upper": upper})
            return out

        coord._build_target_band_for = types.MethodType(build, coord)
        band, hold = _hold_at(coord, NOW)
        assert band == (60.0, 63.0)
        assert hold == {"hold_lower": 63.0, "hold_upper": 80.0}

    def test_no_bank_row_carries_no_hold(self) -> None:
        """Before the bank window the chart draws the band schedule itself: nothing is added to the row."""
        band, hold = _hold_at(_chart_coord(), datetime(2026, 7, 13, 11, 30, tzinfo=UTC))
        assert band == (60.0, 80.0)
        assert hold == {}

    def test_no_time_of_use_schedule_carries_no_hold(self) -> None:
        coord = _chart_coord()
        coord._tou_phase_resolution = None
        _band, hold = _hold_at(coord, NOW)
        assert hold == {}

    def test_deferred_bank_row_records_the_schedule_band(self) -> None:
        """A door pause defers the bank: the thermostat stays on 60-80, which the chart draws and the row records."""
        band, hold = _hold_at(_chart_coord(owns=False), NOW)
        assert band == (77.0, 80.0)
        assert hold == {"hold_lower": 60.0, "hold_upper": 80.0}

    def test_away_widened_period_records_the_widened_edge(self) -> None:
        """During the high-cost period the Away band is widened by its 3 F expansion: the thermostat holds cool 83."""
        band, hold = _hold_at(_chart_coord(), datetime(2026, 7, 13, 17, 30, tzinfo=UTC))
        assert band == (60.0, 80.0)
        assert hold == {"hold_lower": 60.0, "hold_upper": 83.0}

    def test_home_row_after_the_period_carries_no_hold(self) -> None:
        coord = _chart_coord()
        coord._occupancy_mode = OCCUPANCY_HOME
        coord.automation_engine._occupancy_mode = OCCUPANCY_HOME
        _band, hold = _hold_at(coord, datetime(2026, 7, 13, 17, 30, tzinfo=UTC))
        assert hold == {}  # Home is never widened

    def test_celsius_install_stores_internal_fahrenheit(self) -> None:
        coord = _chart_coord()
        coord.config["temp_unit"] = "celsius"
        _band, hold = _hold_at(coord, NOW)
        assert hold == {"hold_lower": 60.0, "hold_upper": 77.0}  # converted only when state_log is built

    def test_row_context_carries_the_hold(self) -> None:
        coord = _chart_coord()
        mod = _coord_mod()
        coord._target_band_schedule = coord._build_target_band_for(NOW, None)
        with (
            patch.object(mod.dt_util, "now", return_value=NOW),
            patch.object(mod.dt_util, "as_local", side_effect=lambda x: x),
        ):
            ctx = coord._read_chart_context()
        assert (ctx["hold_lower"], ctx["hold_upper"]) == (60.0, 77.0)

    def test_a_display_fault_never_breaks_the_row(self) -> None:
        coord = _chart_coord()

        def boom(*_a, **_k):
            raise RuntimeError("display")

        coord._chart_display_band = boom
        _band, hold = _hold_at(coord, NOW)
        assert hold == {}

    def test_no_schedule_yet_carries_no_hold(self) -> None:
        coord = _chart_coord()
        coord._target_band_schedule = None
        assert coord._read_chart_hold() == {}


# ── ChartStateLog: the field, its buckets, its size ────────────────────────────────────────────────────────────────
_T0 = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)


def _log(tmp_path, rows: list[dict]):
    mod = _chart_log_mod()
    with patch.object(mod.dt_util, "now", return_value=_T0 + timedelta(hours=1)):
        log = mod.ChartStateLog(tmp_path)
        for r in rows:
            log.append(hvac="cool", fan=False, indoor=76.0, outdoor=90.0, **r)
    return log


class TestChartLogField:
    def test_written_only_as_a_pair(self, tmp_path) -> None:
        log = _log(
            tmp_path,
            [
                {"ts": _T0.isoformat(), "lower": 77.0, "upper": 80.0, "hold_lower": 60.0, "hold_upper": 77.0},
                {"ts": (_T0 + timedelta(minutes=10)).isoformat(), "lower": 60.0, "upper": 80.0},
                {"ts": (_T0 + timedelta(minutes=20)).isoformat(), "lower": 60.0, "upper": 80.0, "hold_lower": 1.0},
            ],
        )
        bank, plain, half = log._entries
        assert (bank["lower"], bank["upper"], bank["hold_lower"], bank["hold_upper"]) == (77.0, 80.0, 60.0, 77.0)
        assert "hold_lower" not in plain and "hold_upper" not in plain
        assert "hold_lower" not in half and "hold_upper" not in half

    def test_hourly_bucket_averages_the_drawn_band(self, tmp_path) -> None:
        """Same rule as lower/upper (mean, 1 dp); a row without a hold contributes its lower/upper."""
        log = _log(
            tmp_path,
            [
                {"ts": _T0.isoformat(), "lower": 77.0, "upper": 80.0, "hold_lower": 60.0, "hold_upper": 77.0},
                {"ts": (_T0 + timedelta(minutes=30)).isoformat(), "lower": 60.0, "upper": 80.0},
            ],
        )
        (b,) = log._bucket_hourly(log._entries)
        assert (b["lower"], b["upper"]) == (68.5, 80.0)  # unchanged rule and meaning
        assert (b["hold_lower"], b["hold_upper"]) == (60.0, 78.5)

    def test_daily_bucket_same_rule(self, tmp_path) -> None:
        rows = [
            {
                "ts": (_T0 + timedelta(minutes=30 * i)).isoformat(),
                "lower": 77.0,
                "upper": 80.0,
                "hold_lower": 60.0,
                "hold_upper": 77.0,
            }
            for i in range(3)
        ]
        rows.append({"ts": (_T0 + timedelta(hours=3)).isoformat(), "lower": 60.0, "upper": 80.0})
        log = _log(tmp_path, rows)
        (d,) = log._bucket_daily(log._entries)
        assert (d["lower"], d["upper"]) == (72.8, 80.0)
        assert (d["hold_lower"], d["hold_upper"]) == (60.0, 77.8)

    def test_buckets_without_a_hold_row_get_no_keys(self, tmp_path) -> None:
        log = _log(tmp_path, [{"ts": _T0.isoformat(), "lower": 60.0, "upper": 80.0}])
        (h,) = log._bucket_hourly(log._entries)
        (d,) = log._bucket_daily(log._entries)
        assert "hold_lower" not in h and "hold_lower" not in d

    def test_size_budget(self, tmp_path) -> None:
        """docs/chart-log-spec.md Retention Model: an ordinary row is byte-for-byte unchanged; a hold row adds at most
        42 bytes (2 dp values), about 0.12 MB a year for a daily 4-hour bank (8 rows a day)."""
        mod = _chart_log_mod()
        with patch.object(mod.dt_util, "now", return_value=_T0):
            plain = mod.ChartStateLog(tmp_path)
            plain.append(hvac="cool", fan=False, indoor=76.0, outdoor=90.0, lower=77.39, upper=80.0, ts=_T0.isoformat())
            held = mod.ChartStateLog(tmp_path)
            held.append(
                hvac="cool",
                fan=False,
                indoor=76.0,
                outdoor=90.0,
                lower=77.39,
                upper=80.0,
                hold_lower=100.25,  # worst case: 3 digits + 2 dp
                hold_upper=100.25,
                ts=_T0.isoformat(),
            )

        def size(log) -> int:
            return len(json.dumps(log._entries[0], separators=(",", ":")))

        assert "hold_lower" not in plain._entries[0]  # an ordinary row: no new keys, so not one byte more
        added = size(held) - size(plain)
        assert 0 < added <= 42
        assert added * 8 * 365 < 0.15 * 1024 * 1024


# ── get_chart_data(): a historical viewport draws the hold (the REST chart_data body) ───────────────────────────────
_LOWER, _UPPER = 77.0, 80.0  # band schedule (the #786 shape)
_HL, _HU = 60.8, 77.0  # the hold (60.8 F = 16.0 C, 77 F = 25.0 C)


def _historical_chart(tmp_path, range_str: str, temp_unit: str) -> dict:
    mod = _chart_log_mod()
    anchor = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    now_utc = _NOW.replace(tzinfo=UTC)
    with patch.object(mod.dt_util, "now", return_value=now_utc):
        log = mod.ChartStateLog(tmp_path)
        # 06:00-07:30: pre-0.9.6 rows (no hold); 08:00-09:30: bank rows with the hold.
        for i in range(8):
            ts = anchor - timedelta(hours=6) + timedelta(minutes=30 * i)
            extra = {"hold_lower": _HL, "hold_upper": _HU} if i >= 4 else {}
            log.append(
                hvac="cool",
                fan=False,
                indoor=76.0,
                outdoor=90.0,
                lower=_LOWER,
                upper=_UPPER,
                ts=ts.isoformat(),
                **extra,
            )
    coord = _make_full_chart_coord(daily_forecast_full=_daily_6day_forecast(), temp_unit=temp_unit)
    coord._chart_log = log
    coord_mod = _coord_mod()
    now_p, as_local_p = _patched_dt_util(coord_mod, now=now_utc)
    with now_p, as_local_p:
        return coord.get_chart_data(range_str, before_ts=anchor.timestamp())


def _c(f: float) -> float:
    return round((f - 32) * 5 / 9, 1)


@pytest.mark.parametrize("temp_unit", ["fahrenheit", "celsius"])
def test_historical_raw_rows_draw_the_hold(tmp_path, temp_unit) -> None:
    conv = (lambda f: f) if temp_unit == "fahrenheit" else _c
    result = _historical_chart(tmp_path, "24h", temp_unit)
    band = result["target_band"]
    assert len(band) == 8
    old, banked = band[:4], band[4:]
    for b in old:  # rows from before 0.9.6: the old shape
        assert (b["lower"], b["upper"]) == pytest.approx((conv(_LOWER), conv(_UPPER)), abs=0.05)
    for b in banked:
        assert (b["lower"], b["upper"]) == pytest.approx((conv(_HL), conv(_HU)), abs=0.05)
    # state_log (what chart_v2.js reads for the past band) carries both, converted once
    rows = result["state_log"][4:]
    for e in rows:
        assert (e["hold_lower"], e["hold_upper"]) == pytest.approx((conv(_HL), conv(_HU)), abs=0.05)
        assert (e["lower"], e["upper"]) == pytest.approx((conv(_LOWER), conv(_UPPER)), abs=0.05)
    assert all("hold_lower" not in e for e in result["state_log"][:4])


@pytest.mark.parametrize("temp_unit", ["fahrenheit", "celsius"])
@pytest.mark.parametrize("range_str", ["7d", "1y"])
def test_historical_buckets_draw_the_averaged_hold(tmp_path, range_str, temp_unit) -> None:
    conv = (lambda f: f) if temp_unit == "fahrenheit" else _c
    result = _historical_chart(tmp_path, range_str, temp_unit)
    by_ts = {b["ts"]: b for b in result["target_band"]}
    logs = {e["ts"]: e for e in result["state_log"]}
    if range_str == "7d":
        # 06:00 and 07:00 hold no bank row; 08:00 and 09:00 are all bank rows
        assert (by_ts["2026-05-20T06:00:00+00:00"]["lower"]) == pytest.approx(conv(_LOWER), abs=0.05)
        assert (by_ts["2026-05-20T08:00:00+00:00"]["lower"], by_ts["2026-05-20T08:00:00+00:00"]["upper"]) == (
            pytest.approx((conv(_HL), conv(_HU)), abs=0.05)
        )
        assert "hold_lower" not in logs["2026-05-20T06:00:00+00:00"]
    else:
        (day,) = result["target_band"]
        # half the rows banked: mean of the drawn band per row
        assert (day["lower"], day["upper"]) == pytest.approx(
            (conv((_LOWER + _HL) / 2), conv((_UPPER + _HU) / 2)), abs=0.06
        )
        (row,) = result["state_log"]
        assert row["lower"] == pytest.approx(conv(_LOWER), abs=0.05)  # lower/upper keep their meaning


def test_extract_historical_target_band_prefers_a_complete_hold() -> None:
    mod = _coord_mod()
    rows = [
        {"ts": "a", "lower": 77.0, "upper": 80.0, "hold_lower": 60.0, "hold_upper": 77.0},
        {"ts": "b", "lower": 77.0, "upper": 80.0},
        {"ts": "c", "lower": 77.0, "upper": 80.0, "hold_lower": 60.0, "hold_upper": None},
    ]
    assert mod._extract_historical_target_band(rows) == [
        {"ts": "a", "lower": 60.0, "upper": 77.0},
        {"ts": "b", "lower": 77.0, "upper": 80.0},
        {"ts": "c", "lower": 77.0, "upper": 80.0},
    ]


def test_band_entry_at_is_the_lower_upper_bucket() -> None:
    """``_target_band_lower_upper_now()`` and the hold read the same bucket (the refactor kept its rule)."""
    mod = _coord_mod()
    sched = [
        {"ts": datetime(2026, 7, 13, h, 0, tzinfo=UTC).isoformat(), "lower": float(h), "upper": 80.0}
        for h in (13, 14, 15)
    ]
    with patch.object(mod.dt_util, "as_local", side_effect=lambda x: x):
        assert mod._band_entry_at(sched, datetime(2026, 7, 13, 14, 59, tzinfo=UTC))["lower"] == 14.0
        assert mod._band_entry_at(sched, datetime(2026, 7, 13, 12, 0, tzinfo=UTC))["lower"] == 13.0
