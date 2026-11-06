"""The shadow wiring against lane A's real ``predictor_v4`` (Issue #1230 R1), not the fake.

Occupant view: the same promises as test_v4_shadow_wiring, but with the real fit and prediction maths: a fit from
recorded history succeeds, the 24 h prediction is archived, and the per-cycle cost stays small. Skipped while the engine
is not in this tree or at /workspace/ClimateAdvisor-r1-core (the integration merges the branches).
"""

from __future__ import annotations

import importlib.util
import math
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from custom_components.climate_advisor import v4_shadow
from tests.test_v4_shadow_wiring import _START, _bands, _build, _cycle, _hourly


def _load_engine():
    try:
        from custom_components.climate_advisor import predictor_v4

        return predictor_v4
    except ImportError:
        path = Path("/workspace/ClimateAdvisor-r1-core/custom_components/climate_advisor/predictor_v4.py")
        if not path.exists():
            return None
        spec = importlib.util.spec_from_file_location("predictor_v4_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["predictor_v4_under_test"] = mod
        spec.loader.exec_module(mod)
        return mod


ENGINE = _load_engine()
pytestmark = pytest.mark.skipif(ENGINE is None, reason="predictor_v4 not available in this tree")


def _history(coord, days: int = 14) -> None:
    """A heated house: indoor relaxes to outdoor, the thermostat heats at 67 F, 30-minute rows."""
    sid = coord.config["climate_entity"]
    indoor = 69.0
    start = _START - timedelta(days=days)
    for k in range(days * 48):
        ts = start + timedelta(minutes=30 * k)
        outdoor = 38.0 + 10.0 * math.sin((ts.hour + ts.minute / 60 - 9) / 24 * 2 * math.pi)
        heating = indoor < 67.5
        indoor += -0.06 * (indoor - outdoor) * 0.5 + (1.6 * 0.5 if heating else 0.0)
        coord._chart_log.append(
            hvac="heating" if heating else "idle",
            fan=False,
            indoor=round(indoor),
            outdoor=round(outdoor, 1),
            pred_outdoor=round(outdoor + 0.5, 1),
            setpoint=68.0,
            indoor_sensors={sid: round(indoor, 1)},
            ts=ts.isoformat(),
        )


@pytest.fixture
def real(monkeypatch):
    monkeypatch.setattr(v4_shadow, "_predictor", lambda: ENGINE)
    return ENGINE


def test_real_fit_then_cycle_archives_and_is_cheap(real) -> None:
    coord, fake_hass, scheduler = _build()
    _history(coord)
    with scheduler.installed():
        assert coord._v4_start_fit("first")
        scheduler.advance_by(0)
    model = coord.learning._state.v4_model
    assert model is not None, coord._v4_last_fit
    assert real.from_dict(model) is not None  # the persisted dict round-trips through the engine
    assert coord._v4_last_fit["ok"] is True

    # one cycle at "now": a chart row, the hourly forecast and the band
    now = scheduler.now()
    coord._hourly_forecast_temps = _hourly(now)
    coord._target_band_schedule = _bands(now)
    coord._chart_log.append(
        hvac="idle", fan=False, indoor=70.0, outdoor=40.0, pred_outdoor=40.5, setpoint=68.0,
        indoor_sensors={coord.config["climate_entity"]: 70.2}, ts=now.isoformat(),
    )  # fmt: skip
    t0 = time.perf_counter()
    _cycle(coord, scheduler)
    wall_ms = (time.perf_counter() - t0) * 1000
    if model["confidence"] != "none":
        assert coord._v4_last_cycle["predicted"] is True and coord._v4_archive.data
        assert coord._v4_last_cycle["inputs"] == 96
    assert wall_ms < 500, f"shadow cycle took {wall_ms:.0f} ms"
    print(f"v4 shadow cycle: {coord._v4_last_cycle} wall={wall_ms:.1f}ms confidence={model['confidence']}")  # noqa: T201


def test_input_grid_is_stepped_in_utc_across_dst(real) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Denver")
    now = datetime(2026, 11, 1, 0, 40, tzinfo=tz)  # fall back at 02:00 local: the grid must not repeat or skip
    hourly = [(datetime(2026, 11, 1, 0, tzinfo=tz) + timedelta(hours=h), 40.0 + h) for h in range(40)]
    inputs = v4_shadow.build_inputs(
        now=now, hourly=hourly, band=[(now, 68.0, 76.0)], day_modes={}, windows=(False, None, None)
    )
    utc = [i.ts.timestamp() for i in inputs]
    assert all(b - a == 900 for a, b in zip(utc, utc[1:], strict=False))
    assert [i.ts for i in inputs] == real.grid_steps(now, len(inputs))
