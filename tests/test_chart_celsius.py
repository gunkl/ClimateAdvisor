"""Issue #1015 (defect A): Celsius installs plotted the PAST Target Band in Fahrenheit.

``get_chart_data()``'s ``state_log`` carries chart-log entries whose ``lower``/``upper`` are stored
in internal Fahrenheit regardless of the display unit; the frontend draws the historical band
straight from them. ``_conv_log_entry`` converted only the ``pred_*`` keys, so a Celsius occupant
saw last night's comfort band at "61-75" on a degrees-C axis.

These tests drive the real ``get_chart_data()`` (via the shared ``_make_full_chart_coord``
fixture) and the real ``ChartStateLog`` bucketers -- nothing here re-implements the conversion.
"""

from __future__ import annotations

import importlib
import inspect
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from tests.test_forecast_chart_integration import (  # noqa: E402
    _NOW,
    _daily_6day_forecast,
    _get_coordinator_module,
    _make_full_chart_coord,
    _patched_dt_util,
)

_LOWER_F = 69.8  # 21.0 C
_UPPER_F = 75.2  # 24.0 C

# Keys that ChartStateLog.append()/bucketers emit as temperatures but that get_chart_data()
# intentionally leaves in raw internal Fahrenheit inside _conv_log_entry().
_RAW_F_ALLOWLIST = {
    "indoor",  # read pre-conversion into actual_indoor, which is converted separately via _conv()
    "outdoor",  # read pre-conversion into actual_outdoor, which is converted separately via _conv()
    "setpoint",  # fed to _extract_historical_effective_target() AFTER _conv_log_entry, then _conv()'d once
    "nat_vent_target",  # same as setpoint: converting in _conv_log_entry would double-convert
    # Daily-summary (1y) names for the same indoor/outdoor series, same reason as indoor/outdoor:
    "indoor_avg",
    "indoor_min",
    "indoor_max",
    "outdoor_avg",
    "outdoor_min",
    "outdoor_max",
}
# Numeric daily/hourly summary fields that are not temperatures at all.
_NON_TEMPERATURE_NUMERIC = {"fan_minutes"}


def _chart_log_mod():
    return importlib.import_module("custom_components.climate_advisor.chart_log")


def _raw_entry(ts: datetime) -> dict:
    """Shaped exactly like ChartStateLog.append() output (internal Fahrenheit)."""
    return {
        "ts": ts.isoformat(),
        "hvac": "off",
        "fan": False,
        "indoor": 72.0,
        "outdoor": 60.0,
        "windows_open": False,
        "windows_recommended": False,
        "pred_outdoor": 61.0,
        "pred_indoor": 71.0,
        "setpoint": None,
        "fan_running": False,
        "nat_vent_active": False,
        "lower": _LOWER_F,
        "upper": _UPPER_F,
        "nat_vent_target": None,
    }


def _run_chart(coord, range_str: str, now: datetime = _NOW, **kwargs):
    coord_mod = _get_coordinator_module()
    now_p, as_local_p = _patched_dt_util(coord_mod, now=now)
    with now_p, as_local_p:
        return coord.get_chart_data(range_str, **kwargs)


@pytest.mark.parametrize(
    ("temp_unit", "expected_lower", "expected_upper"),
    [("celsius", 21.0, 24.0), ("fahrenheit", _LOWER_F, _UPPER_F)],
)
def test_state_log_band_converted_for_display_unit_24h(temp_unit, expected_lower, expected_upper):
    coord = _make_full_chart_coord(daily_forecast_full=_daily_6day_forecast(), temp_unit=temp_unit)
    coord._chart_log.get_entries.return_value = [_raw_entry(_NOW - timedelta(hours=h)) for h in (3, 2, 1)]

    result = _run_chart(coord, "24h")

    state_log = result["state_log"]
    assert len(state_log) == 3
    for e in state_log:
        assert e["lower"] == pytest.approx(expected_lower, abs=0.05)
        assert e["upper"] == pytest.approx(expected_upper, abs=0.05)

    # Future band must be in the same display unit as the past band (continuity on the axis).
    assert result["target_band"], "fixture must yield a forward target band"
    if temp_unit == "celsius":
        # comfort_heat/cool 68/76 F -> 20.0/24.4 C; a Fahrenheit leak would be >= 60.
        assert all(b["upper"] < 40 and b["lower"] < 40 for b in result["target_band"])
        assert all(e["lower"] < 40 and e["upper"] < 40 for e in state_log)
    else:
        assert all(b["upper"] > 40 for b in result["target_band"])


@pytest.mark.parametrize(
    ("range_str", "temp_unit", "expected_lower", "expected_upper"),
    [
        ("7d", "celsius", 21.0, 24.0),
        ("7d", "fahrenheit", _LOWER_F, _UPPER_F),
        ("1y", "celsius", 21.0, 24.0),
        ("1y", "fahrenheit", _LOWER_F, _UPPER_F),
    ],
)
def test_bucketed_band_converted_for_display_unit(tmp_path, range_str, temp_unit, expected_lower, expected_upper):
    """Real ChartStateLog hourly (7d) / daily (1y) bucketers feed get_chart_data().

    Uses a historical viewport (before_ts well in the past): the real chart-log window is read
    relative to that anchor, no forecast series is built (so the fixture's naive forecast
    datetimes are irrelevant), and the historical-band branch (which reads log_entries BEFORE
    _conv_log_entry) is exercised alongside the state_log conversion.
    """
    chart_log_mod = _chart_log_mod()
    anchor = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    now_utc = _NOW.replace(tzinfo=UTC)
    with patch.object(chart_log_mod.dt_util, "now", return_value=now_utc):
        log = chart_log_mod.ChartStateLog(tmp_path)
        for h in (30, 29, 28, 27):
            log.append(
                hvac="off",
                fan=False,
                indoor=72.0,
                outdoor=60.0,
                pred_outdoor=61.0,
                pred_indoor=71.0,
                lower=_LOWER_F,
                upper=_UPPER_F,
                ts=(anchor - timedelta(hours=h)).isoformat(),
            )
    coord = _make_full_chart_coord(daily_forecast_full=_daily_6day_forecast(), temp_unit=temp_unit)
    coord._chart_log = log
    result = _run_chart(coord, range_str, now=now_utc, before_ts=anchor.timestamp())

    state_log = result["state_log"]
    assert state_log, "bucketers must return at least one bucket"
    for e in state_log:
        assert e["lower"] == pytest.approx(expected_lower, abs=0.05)
        assert e["upper"] == pytest.approx(expected_upper, abs=0.05)
    # The historical band branch converts the same raw values exactly once (no double conversion).
    assert result["target_band"], "historical band must come from the chart-log lower/upper"
    for b in result["target_band"]:
        assert b["lower"] == pytest.approx(expected_lower, abs=0.05)
        assert b["upper"] == pytest.approx(expected_upper, abs=0.05)


# ---------------------------------------------------------------------------
# Structural registry: a new chart-log temperature field must be classified.
# ---------------------------------------------------------------------------


def _numeric_temperature_keys(entry: dict) -> set[str]:
    return {
        k
        for k, v in entry.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool) and k not in _NON_TEMPERATURE_NUMERIC
    }


def test_every_chart_log_temperature_field_is_classified(tmp_path):
    """Call the real append() with every float-typed keyword set to a distinct value, then
    bucket the entries with the real hourly/daily bucketers. Every numeric temperature key in
    any emitted shape must be in _CHART_LOG_CONVERTED_TEMP_KEYS or the raw-F allowlist, so a
    newly added field fails CI until someone decides how get_chart_data() should treat it."""
    coord_mod = _get_coordinator_module()
    chart_log_mod = _chart_log_mod()
    converted = set(coord_mod._CHART_LOG_CONVERTED_TEMP_KEYS)

    float_kwargs = [
        name
        for name, p in inspect.signature(chart_log_mod.ChartStateLog.append).parameters.items()
        if "float" in str(p.annotation)
    ]
    assert {"lower", "upper", "pred_outdoor", "pred_indoor"} <= set(float_kwargs), (
        "signature introspection no longer finds the known temperature keywords"
    )

    now_utc = _NOW.replace(tzinfo=UTC)
    with patch.object(chart_log_mod.dt_util, "now", return_value=now_utc):
        log = chart_log_mod.ChartStateLog(tmp_path)
        values = {name: 50.0 + i for i, name in enumerate(float_kwargs)}
        log.append(
            hvac="off",
            fan=False,
            ts=(now_utc - timedelta(hours=2)).isoformat(),
            **{"indoor": None, "outdoor": None, **values},
        )
        raw_entry = dict(log._entries[0])
        hourly = log._bucket_hourly(log._entries)[0]
        daily = log._bucket_daily(log._entries)[0]

    classified = converted | _RAW_F_ALLOWLIST
    for shape_name, shape in (("raw", raw_entry), ("hourly", hourly), ("daily", daily)):
        unclassified = _numeric_temperature_keys(shape) - classified
        assert not unclassified, (
            f"chart-log {shape_name} entry has temperature field(s) {sorted(unclassified)} that are neither in "
            "coordinator._CHART_LOG_CONVERTED_TEMP_KEYS nor the raw-F allowlist -- decide whether "
            "get_chart_data()'s _conv_log_entry() must convert them (Issue #1015)"
        )

    # Every append() temperature keyword must show up in the raw entry (guards the test itself).
    assert set(float_kwargs) <= set(raw_entry)
    # The Target Band keys the frontend reads from state_log must be converted.
    assert {"lower", "upper"} <= converted
